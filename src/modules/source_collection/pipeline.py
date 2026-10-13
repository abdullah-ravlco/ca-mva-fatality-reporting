
"""Step 1: source collection. Run via src/main.py.

Writes two files in data/source_collection/:
  fetched_sources.json  - everything fetched, with every filter decision
  sources_to_open.json  - one entry per crash to open: confirmed and doubtful
"""

import logging
from collections import Counter

from src.core.date_utils import iso, utc_now
from src.core.http_client import HttpClient
from src.core.logger import configure_logging
from src.modules.source_collection.case_grouping import build_cases
from src.modules.source_collection.collectors.base import collect_from_source
from src.modules.source_collection.collectors.riverside_coroner import collect_riverside_coroner
from src.modules.source_collection.collectors.rss import collect_rss_sources
from src.modules.source_collection.collectors.sb_coroner import collect_sb_coroner
from src.modules.source_collection.deduplication import canonical_url, deduplicate_records
from src.modules.source_collection.final_selection import select_records
from src.modules.source_collection.output_files import (
    write_fetched_sources,
    write_raw_feed_items,
    write_sources_to_open,
)
from src.modules.source_collection.records import make_source_result, refresh_news_status
from src.modules.source_collection.sources import LOOKBACK_HOURS, SOURCES

logger = logging.getLogger(__name__)


COLLECTORS = {
    "sb_coroner": collect_sb_coroner,
    "riverside_coroner": collect_riverside_coroner,
    "listing": collect_from_source,
}

AGGREGATOR_HOSTS = ("news.google.com",)
# Page reads that prove a publisher URL leads to the article text.
VERIFIED_EXTRACTIONS = {"fetched", "fetched_with_browser_headers", "not_needed_feed_has_full_text"}


def normalize_url(url):
    """Absolute http(s) URL, or None ("//host/path" becomes "https://host/path")."""
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    return url if url.startswith(("http://", "https://")) else None


def url_rank(url, record):
    """(rank, url_type, verified) - lower rank is opened first.

    0 official record page (coroner / CHP)
    1 publisher article whose text was read during collection (verified)
    2 publisher article not read (blocked, not fetched, or only known as a duplicate)
    3 Google News redirect (often can't be followed without JavaScript)
    """
    if any(host in url for host in AGGREGATOR_HOSTS):
        return 3, "google_news_redirect", False
    if record and record.get("source_type") in ("coroner", "official_listing"):
        return 0, "official_record", True
    if record and record.get("page_extraction") in VERIFIED_EXTRACTIONS:
        return 1, "publisher_article", True
    return 2, "publisher_article", False


def prioritize_case_urls(cases, records):
    """Order each case's sources best-first and set open_first to the best direct URL.

    Every URL stays in "sources" as an alternative; nothing is dropped.
    """
    by_url = {canonical_url(r.get("url")): r for r in records if r.get("url")}

    for case in cases:
        ranked, seen = [], set()
        for position, source in enumerate(case.get("sources") or []):
            url = normalize_url(source.get("url"))
            key = canonical_url(url) if url else None
            if not url or key in seen:
                continue
            seen.add(key)
            rank, url_type, verified = url_rank(url, by_url.get(key))
            ranked.append((rank, position, dict(source, url=url, url_type=url_type, verified=verified)))

        ranked.sort(key=lambda item: (item[0], item[1]))
        case["sources"] = [source for _, _, source in ranked]
        direct = [s for s in case["sources"] if s["url_type"] != "google_news_redirect"]
        best = direct[0] if direct else (case["sources"][0] if case["sources"] else None)
        case["open_first"] = best["url"] if best else None
    return cases


def label_sources(sources_checked, records):
    """Add a "result" to every requested source: failed | contributed |
    candidates_not_selected | no_relevant_items."""
    used = Counter()
    for record in records:
        if record.get("selection_bucket") == "discard":
            continue
        used[record.get("source_name")] += 1
        for ref in record.get("also_reported_by") or []:
            used[ref.get("source_name")] += 1

    for source in sources_checked:
        failed = source.get("http_status") is None or (source.get("error") and source.get("items_seen", 0) == 0)
        source["records_kept"] = used.get(source["source_name"], 0)
        source["result"] = (
            "failed" if failed
            else "contributed" if source["records_kept"]
            else "candidates_not_selected" if source.get("candidate_count")
            else "no_relevant_items"
        )
    return sources_checked


def collect_official_sources(client, now):
    records, checked = [], []
    for source in SOURCES:
        logger.info("Collecting: %s", source["name"])
        try:
            source_records, result = COLLECTORS[source["type"]](source, client, now, LOOKBACK_HOURS)
        except Exception as exc:  # one broken parser must not stop the run
            source_records = []
            result = make_source_result(source["name"], source["url"], now, source["type"])
            result["error"] = f"collector crashed: {type(exc).__name__}: {exc}"

        logger.info("  %s candidates%s", result["candidate_count"], f" (error: {result['error']})" if result["error"] else "")
        records.extend(source_records)
        checked.append(result)
    return records, checked


def main():
    now = utc_now()
    client = HttpClient()

    # Step 1: official websites (coroner releases, CHP).
    all_records, sources_checked = collect_official_sources(client, now)

    # Step 2: publisher RSS and Google News RSS feeds.
    rss_records, rss_checked = collect_rss_sources(client, now, LOOKBACK_HOURS)
    logger.info("RSS records collected: %s", len(rss_records))
    all_records.extend(rss_records)
    sources_checked.extend(rss_checked)

    # Step 3: remove duplicate articles, then re-check news statuses because
    # merging may have filled in a crash date or location from a duplicate.
    unique_records = deduplicate_records(all_records)
    for record in unique_records:
        refresh_news_status(record, now, LOOKBACK_HOURS)
    duplicates_removed = len(all_records) - len(unique_records)

    # Step 4: decide consider / review / discard for every record.
    consider, review, discarded = select_records(unique_records)

    # Step 5: one case per crash; confirmed vs doubtful.
    confirmed, doubtful, dropped = build_cases(consider, review)

    # Prefer direct publisher/official URLs over Google News redirects; keep the
    # rest of each case's URLs as alternatives in "sources".
    prioritize_case_urls(confirmed + doubtful, unique_records)

    # Step 6: write the output files.
    run_at = iso(now)
    raw_file = write_raw_feed_items(run_at, sources_checked)
    fetched_file = write_fetched_sources(
        run_at, LOOKBACK_HOURS, label_sources(sources_checked, unique_records),
        unique_records, discarded, duplicates_removed,
    )
    to_open_file = write_sources_to_open(run_at, LOOKBACK_HOURS, confirmed, doubtful, dropped)

    logger.info("Collection complete.")
    logger.info("Records collected: %s (duplicates merged: %s)", len(all_records), duplicates_removed)
    logger.info("Records discarded: %s %s", sum(discarded.values()), discarded)
    logger.info("Cases to open - confirmed: %s, doubtful: %s", len(confirmed), len(doubtful))
    if dropped:
        logger.info("Doubtful cases dropped: %s", dropped)
    logger.info("Raw feed items:   %s", raw_file)
    logger.info("Fetched sources:  %s", fetched_file)
    logger.info("Sources to open:  %s", to_open_file)

    if all(source.get("result") == "failed" for source in sources_checked):
        logger.error("Every source failed; nothing was collected.")
        return False
    return True


if __name__ == "__main__":
    configure_logging()
    main()
