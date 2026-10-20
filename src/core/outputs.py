"""Stage output files: one copy per run, plus the stable copy later stages read.

data/runs/<run_id>/<module>/<file>  - kept for every run (history)
data/<module>/<file>                - latest run; the input of the next stage

The JSON files are always written and read here. When a database is configured (.env, see db.py),
every JSON file under data/ is also copied to Postgres' documents table (key = its path relative to
data/); the stage tables are filled separately after each stage (db_sync.py). A database that cannot
be reached never stops a run: the copy is switched off for the rest of the run and a warning is logged.
"""

import csv
import json
import logging
import os
from datetime import datetime, timezone

from config.settings import DATA_DIR, module_data_dir
from src.core import db

RUN_ID_ENV = "PIPELINE_RUN_ID"

logger = logging.getLogger(__name__)
_copy_failed = [False]  # set after the first failed copy, so a down database does not slow every write


def new_run_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_id():
    """The current run's id: set once by src/main.py, or created for a standalone stage run."""
    value = os.environ.get(RUN_ID_ENV)
    if not value:
        value = new_run_id()
        os.environ[RUN_ID_ENV] = value
    return value


def run_dir(module):
    return DATA_DIR / "runs" / run_id() / module


def db_key(path):
    """The Postgres key of a JSON file under data/ (None for any other path or when Postgres is off)."""
    if _copy_failed[0] or not db.enabled():
        return None
    try:
        return path.relative_to(DATA_DIR).as_posix()
    except ValueError:
        return None


def copy_to_database(path, data):
    """Copy one JSON output to Postgres' documents table; a failure is logged and never raised."""
    key = db_key(path)
    if not key:
        return
    try:
        db.put(key, data)
    except Exception as error:
        _copy_failed[0] = True
        logger.warning("Database copy switched off for this run (%s: %s). The JSON files are complete; "
                       "load them later with: python -m src.core.migrate_to_postgres", type(error).__name__, error)


def write_json_atomic(path, data):
    """Write to a temporary file, then replace the destination; also copied to Postgres when configured."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary_path, path)
    copy_to_database(path, data)


def write_stage_json(module, filename, data):
    """Write one stage output to the run folder and the stable folder. Returns the stable path."""
    write_json_atomic(run_dir(module) / filename, data)
    path = module_data_dir(module) / filename
    write_json_atomic(path, data)
    return path


def write_csv_atomic(path, columns, rows):
    """Write rows (dicts) as CSV with a header, via a temporary file. UTF-8 with BOM so Excel opens it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary_path, path)


def write_stage_csv(module, filename, columns, rows):
    """CSV version of write_stage_json: run folder and stable folder. Returns the stable path."""
    write_csv_atomic(run_dir(module) / filename, columns, rows)
    path = module_data_dir(module) / filename
    write_csv_atomic(path, columns, rows)
    return path


def read_json(path, default=None):
    """File contents, or default when the file is missing, empty or not valid JSON."""
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text) if text.strip() else default
    except (OSError, ValueError):
        return default
