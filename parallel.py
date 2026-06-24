"""
parallel.py
-----------
Tiny concurrency helpers used throughout the project.

Why:
  Most data sources (mfapi.in, Yahoo, IBJA, mintbyte) are I/O-bound. A
  ThreadPoolExecutor lets us fetch N things in parallel with ~1 line and
  zero risk of GIL issues (network calls release the GIL).

Design choices:
  - Pure stdlib (concurrent.futures + requests). No aiohttp dependency.
  - ``map_parallel`` is the workhorse; it preserves input order in the
    returned list.
  - Per-task exceptions are logged and surfaced as ``None`` so one bad
    URL doesn't kill the whole batch.
  - ``workers`` defaults to 8, which is enough for typical portfolios
    (<30 holdings) without overwhelming the upstream APIs.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, TypeVar

from logging_setup import get_logger

log = get_logger("parallel")

T = TypeVar("T")
R = TypeVar("R")


# Default workers: respect env override, then 8.
DEFAULT_WORKERS = int(os.getenv("PT_PARALLEL_WORKERS", "8"))


def map_parallel(
    func: Callable[[T], R],
    items: Iterable[T],
    *,
    workers: int = DEFAULT_WORKERS,
    desc: str = "items",
    on_error: Callable[[T, BaseException], R] | None = None,
) -> list[R]:
    """
    Apply ``func`` to every item in ``items`` in parallel.

    Returns the results in the same order as the input. Items where ``func``
    raises are logged; if ``on_error`` is supplied it's called to produce
    a fallback result, otherwise ``None`` is returned in that slot.

    Args:
        func:     any callable taking a single item.
        items:    iterable of inputs.
        workers:  thread-pool size (default 8; PT_PARALLEL_WORKERS overrides).
        desc:     short label used in the log line ("fetched 5/8 {desc}").
        on_error: optional fallback function ``on_error(item, exc) -> result``.

    Example:
        prices = map_parallel(_fetch_one, tickers, desc="tickers")
    """
    items = list(items)
    if not items:
        return []
    results: list[R | None] = [None] * len(items)
    failures = 0

    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        future_to_idx = {pool.submit(func, item): i for i, item in enumerate(items)}
        done = 0
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            done += 1
            try:
                results[idx] = fut.result()
            except BaseException as e:
                failures += 1
                log.warning("[%d/%d] %s failed: %s", done, len(items), desc, e)
                if on_error is not None:
                    try:
                        results[idx] = on_error(items[idx], e)
                    except Exception as ee:
                        log.error("on_error handler itself failed: %s", ee)

    log.info("%s: %d/%d ok, %d failed", desc, len(items) - failures, len(items), failures)
    return results  # type: ignore[return-value]


def fetch_all(
    fetchers: dict[str, Callable[[], R]],
    *,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, R]:
    """
    Run several independent ``fetchers`` in parallel. Useful for the daily
    pipeline where indices, MF NAVs and SGB prices have no inter-dependency.

    Returns ``{name: result}``. Each entry is guaranteed to exist (set to
    None if the fetcher raised).

    Example:
        data = fetch_all({
            "indices": lambda: fetch_indices("5d"),
            "sgb":     lambda: fetch_sgb_rows(load_sgbs()),
            "mf":      lambda: fetch_mf_rows(load_mfs()),
        })
    """
    names = list(fetchers.keys())
    funcs = [fetchers[n] for n in names]
    out_list = map_parallel(lambda f: f(), funcs, workers=workers, desc="fetchers")
    return dict(zip(names, out_list))