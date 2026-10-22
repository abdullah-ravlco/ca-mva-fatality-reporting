"""Step 1: source collection. Run via src/main.py.

Writes two files in data/source_collection/:
  all_raw.json       - every item from RSS feeds, Google News and Tavily, unfiltered
  finalized_raw.json - after removing same-link duplicates, low-value sources, off-topic,
                       out-of-California and old stories; Google News links resolved to the
                       publisher URL
Every item has: published_date, source_name, title, url, description.
"""

import hashlib
import logging

from config.settings import DATA_DIR
from src.core.checkpoint import Checkpoint
from src.core.date_utils import iso, reference_time, utc_now
from src.core.http_client import HttpClient
from src.core.logger import configure_logging
from src.modules.source_collection.finalize import drop_google_twins, finalize, remove_same_link
from src.modules.source_collection.history import build_entry, window_start, write_history
from src.modules.source_collection.output_files import write_all_raw, write_finalized_raw
from src.modules.source_collection.raw_collection import collect_rss, collect_tavily
from src.modules.source_collection.resolve_links import resolve_google_links
from src.modules.source_collection.sources import (
    GOOGLE_NEWS_DELAY_SECONDS, GOOGLE_NEWS_QUERIES, TAVILY_QUERIES,
)

logger = logging.getLogger(__name__)

CHECKPOINT_FILE = DATA_DIR / "state" / "collection_progress.jsonl"
# A checkpoint older than this is not resumed.
CHECKPOINT_MAX_AGE_HOURS = 6


def collection_checkpoint(since):
    """Finished searches of an interrupted run for the same window start and queries, if any."""
    queries = "|".join(GOOGLE_NEWS_QUERIES + TAVILY_QUERIES)
    signature = f"{iso(since)}|{hashlib.sha1(queries.encode()).hexdigest()[:12]}"
    checkpoint = Checkpoint(CHECKPOINT_FILE, signature, CHECKPOINT_MAX_AGE_HOURS)
    if checkpoint.items:
        logger.info("Resuming an interrupted run: %s searches already finished", len(checkpoint.items))
    return checkpoint


PARTIAL_EVERY = 10  # rewrite all_raw.json (marked incomplete) after this many more finished searches


def partial_all_raw_writer(run_at, checkpoint):
    """A callback that keeps all_raw.json up to date while the searches are running."""
    state = {"finished": 0}

    def write_partial():
        state["finished"] += 1
        if state["finished"] % PARTIAL_EVERY == 0:
            records = [record for payload in checkpoint.snapshot() for record in payload["records"]]
            write_all_raw(run_at, records, [], complete=False)

    return write_partial


def main(since=None):
    """since: where the window starts (a date); default is the start of the last run."""
    run_at = iso(utc_now())
    now = reference_time()
    start = window_start(since)
    logger.info("Collection window: %s to %s", iso(start), iso(now))

    checkpoint = collection_checkpoint(start)
    on_progress = partial_all_raw_writer(run_at, checkpoint)
    rss_records, rss_errors = collect_rss(
        HttpClient(host_delays={"news.google.com": GOOGLE_NEWS_DELAY_SECONDS}), start, now, checkpoint, on_progress
    )
    tavily_records, tavily_errors = collect_tavily(start, checkpoint=checkpoint, on_progress=on_progress)
    all_raw = rss_records + tavily_records
    errors = rss_errors + tavily_errors
    logger.info("Raw items: %s (RSS %s, Tavily %s)", len(all_raw), len(rss_records), len(tavily_records))
    all_raw_file = write_all_raw(run_at, all_raw, errors)
    if not errors:
        checkpoint.clear()  # failed searches stay out of the checkpoint, so only they are retried

    finalized, dropped = finalize(all_raw, now, start)
    # Save the filtered items now, with the links as collected: if the slow link lookup below is
    # interrupted, stage 2 can already use this file, and the lookups done so far are cached.
    write_finalized_raw(run_at, start, finalized, {"note": "links not resolved yet", **dropped})

    # Publisher URLs instead of Google News redirects (all_raw.json keeps the collected link);
    # the same article found by two routes is now the same link.
    finalized, dropped["duplicate_story_google_copy"] = drop_google_twins(finalized)
    google_links = resolve_google_links(finalized)
    finalized, same_after = remove_same_link(finalized)
    dropped["duplicate_links_after_resolution"] = len(same_after)

    entry = build_entry(run_at, len(all_raw), dropped, len(finalized), google_links)
    finalized_file = write_finalized_raw(run_at, start, finalized, entry)
    history_file = write_history(entry)

    logger.info("Run history: %s", entry)
    logger.info("All raw:        %s", all_raw_file)
    logger.info("Finalized raw:  %s", finalized_file)
    logger.info("Run history:    %s", history_file)

    if not all_raw:
        logger.error("Every source failed; nothing was collected.")
        return False
    return True


if __name__ == "__main__":
    configure_logging()
    main()
