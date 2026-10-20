"""Load the existing JSON files under data/ into Postgres, and check the connection.

    .venv/bin/python -m src.core.migrate_to_postgres            copy every JSON file + fill the tables
    .venv/bin/python -m src.core.migrate_to_postgres --check    only test the connection and create the tables
    .venv/bin/python -m src.core.migrate_to_postgres --dry-run  list what would be copied

Needs a database in .env (DATABASE_URL, or PGHOST/PGDATABASE/...; see src/core/db.py). Safe to repeat:
documents are overwritten with the file and the tables are upserted. CSV files, .jsonl checkpoints and
the page-fetch cache stay as files.
"""

import json
import logging
import sys

from config.settings import DATA_DIR
from src.core import db
from src.core.logger import configure_logging

logger = logging.getLogger(__name__)
SKIP_NAMES = {"fetch_cache.json"}


def check():
    """Connect, apply the migrations, and report the server. True when it works."""
    try:
        version = db.server_version()
    except Exception as error:
        logger.error("Could not connect to Postgres (%s): %s: %s", db.describe(), type(error).__name__, error)
        logger.error("Check DATABASE_URL (or PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGSSLMODE) in .env")
        return False
    tables = [row[0] for row in db._run(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name",
        (db.schema_name(),)).fetchall()]
    logger.info("Connected to Postgres %s at %s; tables: %s", version, db.describe(), ", ".join(tables))
    return True


def main(dry_run=False, check_only=False):
    if not db.enabled():
        logger.error("No database configured: set DATABASE_URL (or PGHOST and PGDATABASE) in .env")
        return False
    if not dry_run and not check():
        return False
    if check_only:
        return True

    copied = skipped = 0
    for path in sorted(DATA_DIR.rglob("*.json")):
        key = path.relative_to(DATA_DIR).as_posix()
        if path.name in SKIP_NAMES or path.name.endswith(".tmp"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.warning("skipped %s: %s", key, error)
            skipped += 1
            continue
        if not dry_run:
            db.put(key, data)
        copied += 1
    logger.info("%s %s file(s) into the documents table, skipped %s", "Would copy" if dry_run else "Copied", copied, skipped)

    if not dry_run:
        from src.core import db_sync

        for stage, counts in db_sync.sync_all().items():
            logger.info("Tables filled from %s: %s", stage, counts or "no output yet")
    return True


if __name__ == "__main__":
    configure_logging()
    sys.exit(0 if main("--dry-run" in sys.argv, "--check" in sys.argv) else 1)
