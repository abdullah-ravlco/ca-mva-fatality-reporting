"""Write the JSON files back into data/ from Postgres' documents table (the reverse of migrate_to_postgres).

    .venv/bin/python -m src.core.restore_from_postgres             restore the files that are missing in data/
    .venv/bin/python -m src.core.restore_from_postgres --dry-run   list what would be restored
    .venv/bin/python -m src.core.restore_from_postgres --overwrite also replace files that exist
    .venv/bin/python -m src.core.restore_from_postgres --include-runs   also restore data/runs/<run_id>/ copies

Use it after data/ was deleted, so the next run continues from the earlier results instead of starting over.
Files that exist are kept unless --overwrite is given. CSV files and .jsonl checkpoints are not in the
database; they are rebuilt by the next run.
"""

import json
import logging
import os
import sys

from config.settings import DATA_DIR
from src.core import db
from src.core.logger import configure_logging

logger = logging.getLogger(__name__)


def target_path(key):
    """data/<key>, or None when the key would leave the data folder."""
    path = (DATA_DIR / key).resolve()
    return path if DATA_DIR.resolve() in path.parents else None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary_path, path)


def main(dry_run=False, overwrite=False, include_runs=False):
    if not db.enabled():
        logger.error("No database configured: set DATABASE_URL (or PGHOST and PGDATABASE) in .env")
        return False
    restored = kept = 0
    for key in db.keys():
        path = target_path(key)
        if path is None or not key.endswith(".json") or (key.startswith("runs/") and not include_runs):
            continue
        if path.exists() and not overwrite:
            kept += 1
            continue
        if not dry_run:
            write_json(path, db.get(key))
        restored += 1
        logger.info("%s %s", "would restore" if dry_run else "restored", key)
    logger.info("%s %s file(s); %s already in data/ were kept", "Would restore" if dry_run else "Restored", restored, kept)
    return True


if __name__ == "__main__":
    configure_logging()
    flags = set(sys.argv[1:])
    sys.exit(0 if main("--dry-run" in flags, "--overwrite" in flags, "--include-runs" in flags) else 1)
