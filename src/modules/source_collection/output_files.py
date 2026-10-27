"""Write the stage output files (to data/source_collection/ and data/runs/<run_id>/).

all_raw.json       - everything fetched from RSS and Tavily, unfiltered, five fields per item:
                     published_date, source_name, title, url, description. It is rewritten
                     while the searches run (complete: false) and finished at the end.
finalized_raw.json - the same fields after removing same-link duplicates, stories that are
                     not about a deadly road crash, and stories older than the window.
                     It grows: every run adds its new items to the ones of earlier runs
"""

from config.settings import module_data_dir
from src.core.date_utils import iso
from src.core.outputs import read_json, write_stage_json
from src.modules.source_collection.deduplication import canonical_url


MODULE = "source_collection"
ALL_RAW_FILE = "all_raw.json"
FINALIZED_FILE = "finalized_raw.json"


def write_all_raw(run_at, records, errors, complete=True):
    """complete=False marks a file written while the searches are still running."""
    return write_stage_json(MODULE, ALL_RAW_FILE, {
        "run_at": run_at,
        "complete": complete,
        "total_items": len(records),
        "source_errors": errors,
        "items": records,
    })


def config_path():
    return module_data_dir(MODULE) / FINALIZED_FILE


def previous_finalized():
    """The items of earlier runs (empty on the first run)."""
    data = read_json(config_path(), default={}) or {}
    return [item for item in data.get("items") or [] if isinstance(item, dict)]


def merge_finalized(previous, new):
    """Earlier items first, then the new ones whose link is not there yet."""
    seen = {canonical_url(item.get("url")) for item in previous}
    merged = list(previous)
    for item in new:
        key = canonical_url(item.get("url"))
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def write_finalized_raw(run_at, window_start, records, summary):
    return write_stage_json(MODULE, FINALIZED_FILE, {
        "run_at": run_at,
        "window_start": iso(window_start),
        "total_items": len(records),
        "summary": summary,
        "items": records,
    })
