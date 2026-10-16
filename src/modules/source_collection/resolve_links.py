"""Replace Google News redirect links with the publisher's article URL.

Only applied to finalized_raw.json: all_raw.json keeps the link exactly as collected. A link
that cannot be resolved is left unchanged. Results are cached in
data/state/google_links.json so each Google link is looked up once, ever.
"""

import logging

from config.settings import DATA_DIR
from src.core.parallel import parallel_map
from src.core.outputs import read_json, write_json_atomic
from src.modules.source_collection.sources import LINK_RESOLVE_WORKERS
from src.modules.link_extraction.google_news_decoder import (
    GoogleNewsDecodeError, decode_google_news_url, is_google_news_url,
)

logger = logging.getLogger(__name__)

CACHE_FILE = DATA_DIR / "state" / "google_links.json"


def resolve_google_links(records, decode=decode_google_news_url, workers=LINK_RESOLVE_WORKERS):
    """Set record["url"] to the publisher URL where possible. Returns (resolved, failed) counts.

    Links are looked up in parallel (the decoder spaces its calls to Google). Only successful
    lookups are cached; a failed one is tried again next run.
    """
    cache = read_json(CACHE_FILE, default={}) or {}
    pending = [r for r in records if is_google_news_url(r.get("url"))]
    todo = sorted({r["url"] for r in pending if not cache.get(r["url"])})

    def lookup(link):
        try:
            return link, decode(link)
        except GoogleNewsDecodeError as error:
            logger.warning("Could not resolve %s: %s", link, error)
            return link, None

    if todo:
        logger.info("Resolving %s Google News links (%s cached)", len(todo), len(pending) - len(todo))
        try:
            for done, (link, target) in enumerate(parallel_map(lookup, todo, workers), start=1):
                if target:
                    cache[link] = target
                if done % 25 == 0:
                    write_json_atomic(CACHE_FILE, cache)
                    logger.info("Resolved Google News links: %s/%s", done, len(todo))
        finally:
            write_json_atomic(CACHE_FILE, cache)  # keep what was resolved even if the run is stopped

    resolved = failed = 0
    for record in pending:
        target = cache.get(record["url"])
        if target and not is_google_news_url(target):
            record["url"] = target
            resolved += 1
        else:
            failed += 1
    return resolved, failed
