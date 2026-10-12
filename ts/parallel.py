"""Parallelise the market-data scan.

The scanner is the dominant cost of every agent cycle: it fetches ~15 symbols
sequentially, which measured 4.8-5.7s. The fetches are independent, so a small
thread pool collapses that wall-clock time to roughly the slowest single fetch.

Errors are still isolated per symbol -- one bad ticker must not fail the sweep
-- and the published order is kept deterministic so the dashboard and the tests
see the same sequence for the same input.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)


def parallel_map(func, items, max_workers: int = 8):
    """Run ``func`` over ``items`` on a thread pool, preserving input order.

    Returns a list aligned with ``items``; entries are ``None`` where ``func``
    raised. A failure in one item never affects the others.
    """
    items = list(items)
    if not items:
        return []
    if len(items) == 1 or max_workers <= 1:
        return [_safe_call(func, item) for item in items]

    workers = min(max_workers, len(items))
    results: list = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_safe_call, func, item): index
                   for index, item in enumerate(items)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 - defensive; _safe_call
                # already catches, this is a last resort
                log.debug("worker failed: %s", exc)
                results[index] = None
    return results


def _safe_call(func, item):
    try:
        return func(item)
    except Exception as exc:  # noqa: BLE001 - isolate every failure
        log.debug("item failed: %s", exc)
        return None
