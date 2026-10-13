
"""Write the two output files.

fetched_sources.json - everything that was fetched: every page/feed requested (with
                       errors), every item skipped by a filter (with reason), and every
                       collected record with its selection decision.
sources_to_open.json - only the cases to open, one entry per crash:
                       "confirmed" (verified from metadata) and "doubtful"
                       (not verifiable from metadata, but not known to be unneeded).
"""

import json
from collections import Counter

from config.settings import module_data_dir


DATA_DIR = module_data_dir("source_collection")
FETCHED_FILE = DATA_DIR / "fetched_sources.json"
TO_OPEN_FILE = DATA_DIR / "sources_to_open.json"


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def write_fetched_sources(run_at, lookback_hours, sources, records, discarded, duplicates_removed):
    buckets = Counter(record.get("selection_bucket") for record in records)
    _write(FETCHED_FILE, {
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
    return FETCHED_FILE


def write_sources_to_open(run_at, lookback_hours, confirmed, doubtful, dropped):
    _write(TO_OPEN_FILE, {
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
    return TO_OPEN_FILE
