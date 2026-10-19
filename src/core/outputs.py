"""Stage output files: one copy per run, plus the stable copy later stages read.

data/runs/<run_id>/<module>/<file>  - kept for every run (history)
data/<module>/<file>                - latest run; the input of the next stage
"""

import csv
import json
import os
from datetime import datetime, timezone

from config.settings import DATA_DIR, module_data_dir

RUN_ID_ENV = "PIPELINE_RUN_ID"


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


def write_json_atomic(path, data):
    """Write to a temporary file, then replace the destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary_path, path)


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
