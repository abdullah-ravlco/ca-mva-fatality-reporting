"""Run history: statistics of what each run collected, added and dropped (no article lists).

Written two ways:
  data/history/run_history.json                         - every run, oldest first (appended)
  data/runs/<run_id>/source_collection/run_history.json - this run only
"""

from config.settings import DATA_DIR
from src.core.outputs import read_json, run_id, write_json_atomic, write_stage_json


HISTORY_FILE = DATA_DIR / "history" / "run_history.json"
MODULE = "source_collection"


REASONS = (
    "duplicate_links", "source_law_firm", "source_social_media", "source_aggregator", "not_on_topic",
    "outside_california", "published_before_window", "duplicate_story_google_copy",
    "duplicate_links_after_resolution",
)


def build_entry(run_at, collected, dropped, finalized_count, google_links=None):
    """dropped: counts by reason from finalize(); google_links: (resolved, failed) or None."""
    entry = {"run_id": run_id(), "run_at": run_at, "all_raw": collected}
    entry.update({reason: dropped.get(reason, 0) for reason in REASONS})
    entry["finalized_raw"] = finalized_count
    if google_links is not None:
        entry["google_links_resolved"], entry["google_links_unresolved"] = google_links
    return entry


def write_history(entry):
    """Save the entry for this run and append it to the all-runs history. Returns the history path."""
    write_stage_json(MODULE, "run_history.json", entry)
    history = read_json(HISTORY_FILE, default=[])
    history = [e for e in history if isinstance(history, list) and e.get("run_id") != entry["run_id"]]
    history.append(entry)
    write_json_atomic(HISTORY_FILE, history)
    return HISTORY_FILE
