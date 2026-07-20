"""
webapp.cache
------------
Tiny mtime-based cache for the JSON files we re-read on every page
load. Goal: keep the per-request disk I/O bounded to O(1) reads even
when the webapp's request handler is hammered.

Why mtime-based, not TTL-based?
  - A fixed TTL (e.g. 5s) forces a re-read on every request once the
    TTL expires — pointless when the file hasn't actually changed.
  - A fixed TTL can also serve stale data: if the scheduler writes
    the file 0.5s after the TTL starts, the next reader still sees
    the old value.
  - An mtime-based cache reads the file once, caches the (value, mtime)
    pair, and on every subsequent read checks only the mtime (a
    single os.stat() call). If the mtime changed, re-read; if not,
    return the cached value. Zero disk I/O on the hot path.

What it caches:
  - `data/portfolio_truth.json` — the user's positions
  - `data/alerts/shareholding/prev.json` — shareholding pattern per ticker
  - `data/cache/mf_master_cache.json` — mfapi.in scheme master list

Capacity: bounded by file count (3 files), so the "LRU" aspect is
defensive (in case we add more cached files later, we don't grow
unbounded). The default cap of 100 entries is plenty for this use case.

Thread-safety: a single threading.Lock guards all cache reads. Webapp
uses a single-process async server (uvicorn) so the lock is rarely
contended, but it's the right primitive for shared state.

Limitations (by design):
  - This is a per-process cache. If you run multiple workers (e.g.
    `uvicorn --workers 4`), each worker has its own copy. Acceptable
    for this project — the files are small and re-reading is cheap.
  - We don't try to handle write-then-read races. The webapp never
    writes these files (only the scheduler does, and the next request
    from the same worker will see the new mtime). Cross-process
    invalidation is the user's job (e.g. touch a flag file).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger("webapp.cache")


class LruJsonCache:
    """
    LRU + mtime-based cache for JSON files.

    - `max_entries` bounds the cache size (oldest entry evicted on overflow).
    - `get(path, default=None)` returns the parsed JSON, refreshing the
      cache if the file's mtime has changed since the cached read.
    - `invalidate(path)` drops a single entry (e.g. after a manual write
      in tests).

    The cache is read-mostly: every call to `get()` does at most one
    `os.stat()` (for the mtime check) and at most one file read (only
    if the mtime changed). On the hot path where the file is unchanged
    between calls, it's `O(1)` with a single stat() call.
    """

    def __init__(self, max_entries: int = 100) -> None:
        self._max = max_entries
        # _data: path -> (mtime, value). OrderedDict so we can pop the
        # oldest entry on overflow. Insertion order = access order
        # (we move-to-end on every get()).
        from collections import OrderedDict
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = Lock()

    def get(self, path: str | Path, default: Any = None) -> Any:
        """
        Return the parsed JSON for `path`, refreshing the cache entry
        if the file's mtime has changed since last read.

        File-missing semantics: if the file is gone, return the cached
        value if we have one (the file might be momentarily inaccessible
        during a write — don't break the page). Only return `default`
        if we have no cache entry at all.
        """
        path_str = str(path)
        with self._lock:
            cached = self._data.get(path_str)

            try:
                mtime = os.path.getmtime(path)
            except OSError:
                # File missing or unreadable. If we have a cache entry,
                # keep using it — the file may be briefly inaccessible
                # during a write. If we have nothing, return default.
                return cached[1] if cached is not None else default

            if cached is not None and cached[0] == mtime:
                # mtime unchanged — return cached value, refresh LRU order
                self._data.move_to_end(path_str)
                return cached[1]

            # mtime changed (or first read). Read the file.
            try:
                with open(path, "r", encoding="utf-8") as f:
                    value = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                log.warning("LruJsonCache: failed to read %s: %s", path, e)
                # Don't cache a failed read — drop the entry too
                self._data.pop(path_str, None)
                return default

            self._data[path_str] = (mtime, value)
            # Enforce max size: drop the LRU entry if we're over.
            while len(self._data) > self._max:
                self._data.popitem(last=False)  # last=False = oldest
            return value

    def invalidate(self, path: str | Path) -> None:
        """Drop a single entry (e.g. after writing)."""
        with self._lock:
            self._data.pop(str(path), None)

    def clear(self) -> None:
        """Drop everything (used by tests)."""
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


# Module-level singleton cache. We share one across the three callers
# (truth.json, prev.json, mf_master_cache.json) so the LRU is global.
_json_cache = LruJsonCache(max_entries=100)


def cached_json(path: str | Path, default: Any = None) -> Any:
    """Module-level convenience: read `path` through the shared cache."""
    return _json_cache.get(path, default)


def invalidate_json(path: str | Path) -> None:
    """Module-level convenience: drop `path` from the shared cache."""
    _json_cache.invalidate(path)


def clear_json_cache() -> None:
    """Module-level convenience: drop everything (used by tests)."""
    _json_cache.clear()
