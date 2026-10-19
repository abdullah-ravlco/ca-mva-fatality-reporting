"""Run work in a thread pool, stopping promptly when the run is interrupted."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor

QUEUED_PER_WORKER = 2  # work submitted ahead of the results being read


def parallel_map(function, items, workers):
    """Yield function(item) for each item, in order, using `workers` threads.

    Only a few items per worker are queued ahead, so when the caller stops early or is interrupted
    (Ctrl+C, an error) little work is left to finish; queued work that has not started is cancelled.
    """
    pool = ThreadPoolExecutor(max_workers=workers)
    pending = deque()
    iterator = iter(items)
    try:
        for item in iterator:
            pending.append(pool.submit(function, item))
            if len(pending) >= workers * QUEUED_PER_WORKER:
                break
        while pending:
            result = pending.popleft().result()
            for item in iterator:  # top the queue up by one
                pending.append(pool.submit(function, item))
                break
            yield result
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
