"""Run work in a thread pool, stopping promptly when the run is interrupted."""

from concurrent.futures import ThreadPoolExecutor


def parallel_map(function, items, workers):
    """Yield function(item) for each item, in order, using `workers` threads.

    If the caller stops early or is interrupted (Ctrl+C, an error), work that has not started
    is cancelled instead of being run to the end; only tasks already running finish.
    """
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        yield from pool.map(function, items)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
