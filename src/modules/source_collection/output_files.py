
"""Write the stage output files (to data/source_collection/ and data/runs/<run_id>/).

fetched_sources.json - everything that was fetched: every page/feed requested (with
                       errors), every item skipped by a filter (with reason), and every
                       collected record with its selection decision.
raw_feed_items.json  - every feed item exactly as the feed gave it, before any filter,
                       with the feed and search query that returned it.
sources_to_open.json - only the cases to open, one entry per crash:
                       "confirmed" (verified from metadata) and "doubtful"
                       (not verifiable from metadata, but not known to be unneeded).
"""

from collections import Counter

from src.core.outputs import write_stage_json


MODULE = "source_collection"
FETCHED_FILE = "fetched_sources.json"
RAW_ITEMS_FILE = "raw_feed_items.json"
TO_OPEN_FILE = "sources_to_open.json"


def write_raw_feed_items(run_at, sources):
    """Move each source's raw_items out of the source results into their own file."""
    items = []
    for source in sources:
        items.extend(source.pop("raw_items", None) or [])
    return write_stage_json(MODULE, RAW_ITEMS_FILE, {
        "run_at": run_at,
        "total_items": len(items),
        "items": items,
    })


def write_fetched_sources(run_at, lookback_hours, sources, records, discarded, duplicates_removed):
    buckets = Counter(record.get("selection_bucket") for record in records)
    return write_stage_json(MODULE, FETCHED_FILE, {
        "run_at": run_at,
        "lookback_hours": lookback_hours,
        "sources": sources,
        "collected_records": records,
        "summary": {
            "sources_requested": len(sources),
            "sources_failed": sum(1 for s in sources if s.get("result") == "failed"),
            "items_seen": sum(s.get("items_seen", 0) for s in sources),
            "items_skipped_by_filters": sum(len(s.get("skipped_items") or []) for s in sources),
            "duplicates_merged": duplicates_removed,
            "collected_records": len(records),
            "records_by_decision": dict(buckets),
            "discarded_by_reason": discarded,
        },
    })


def write_sources_to_open(run_at, lookback_hours, confirmed, doubtful, dropped):
    return write_stage_json(MODULE, TO_OPEN_FILE, {
        "run_at": run_at,
        "lookback_hours": lookback_hours,
        "confirmed": confirmed,
        "doubtful": doubtful,
        "summary": {
            "confirmed_cases": len(confirmed),
            "doubtful_cases": len(doubtful),
            "urls_in_confirmed": sum(len(case["sources"]) for case in confirmed),
            "urls_in_doubtful": sum(len(case["sources"]) for case in doubtful),
            "doubtful_dropped_by_reason": dropped,
        },
    })
