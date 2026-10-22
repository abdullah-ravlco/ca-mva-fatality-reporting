"""Write the stage output files (to data/source_collection/ and data/runs/<run_id>/).

all_raw.json       - everything fetched from RSS and Tavily, unfiltered, five fields per item:
                     published_date, source_name, title, url, description. It is rewritten
                     while the searches run (complete: false) and finished at the end.
finalized_raw.json - the same fields after removing same-link duplicates, stories that are
                     not about a deadly road crash, and stories older than the window
"""

from src.core.date_utils import iso
from src.core.outputs import write_stage_json


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


def write_finalized_raw(run_at, window_start, records, summary):
    return write_stage_json(MODULE, FINALIZED_FILE, {
        "run_at": run_at,
        "window_start": iso(window_start),
        "total_items": len(records),
        "summary": summary,
        "items": records,
    })
