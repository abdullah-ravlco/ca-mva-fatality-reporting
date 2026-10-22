"""Application CLI entrypoint & orchestrator.

Runs the pipeline stages in order:
  1. source_collection  - collect items (Google News RSS, publisher RSS, Tavily) into
                          all_raw.json; filter them into finalized_raw.json
  2. link_extraction    - open every item of finalized_raw.json, read text + page metadata,
                          re-check crash date / location / relevance on the full text
  3. incident_grouping  - group the kept articles into incidents (no LLM)
  4. severity_screening - JEV reads each incident; only catastrophic injuries caused by another
                          party go on (qualified / identity or fault unidentified / needs review)
  5. lead_verification  - an agent investigates each incident that passed screening
  6. contact_enrichment - SearchBug: the deceased person's date of birth, address and phones, and
                          their relatives' names, relationships, addresses and phones

Each stage reads the previous stage's file in data/<stage>/; every run is also kept
in data/runs/<run_id>/. The run stops at the first stage that fails, so later stages
never run on stale input.

Usage:
  python -m src.main                 # run every stage, in order
  python -m src.main --stage STAGE   # run one stage (repeatable)
  python -m src.main --since DATE    # first run only: where collection starts (e.g. 2026-10-01)

Collection covers the time from the start of the last run up to now; there is no fixed lookback.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from src.core import db
from src.core.logger import configure_logging
from src.core.outputs import RUN_ID_ENV, new_run_id
from src.modules.contact_enrichment import pipeline as contact_enrichment
from src.modules.incident_grouping import pipeline as incident_grouping
from src.modules.lead_verification import pipeline as lead_verification
from src.modules.link_extraction import pipeline as link_extraction
from src.modules.severity_screening import pipeline as severity_screening
from src.modules.source_collection import pipeline as source_collection

STAGES = {
    "source_collection": source_collection.main,
    "link_extraction": link_extraction.main,
    "incident_grouping": incident_grouping.main,
    "severity_screening": severity_screening.main,
    "lead_verification": lead_verification.main,
    "contact_enrichment": contact_enrichment.main,
}

STAGE_ORDER = list(STAGES)

logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Run the scraper pipeline.")
    parser.add_argument(
        "--stage",
        choices=STAGE_ORDER,
        action="append",
        dest="stages",
        help="Run only this stage (repeatable). Default: run every stage, in order.",
    )
    parser.add_argument(
        "--since",
        help="Where source collection starts (a date, e.g. 2026-10-01). Needed on the first run; "
             "later runs start where the last one did.",
    )
    return parser.parse_args()


def run_stage(stage, **options):
    """True when the stage finished successfully."""
    try:
        return STAGES[stage](**options) is not False
    except Exception:
        logger.exception("Stage %s failed", stage)
        return False


def save_to_database(stage, started_at, ok):
    """Load the stage's JSON output into the Postgres tables (when a database is configured).

    A database problem is logged and never stops the run: the JSON files are complete, and
    `python -m src.core.migrate_to_postgres` loads them later."""
    if not db.enabled():
        return
    from src.core import db_sync  # only needed with a database

    try:
        counts = db_sync.sync_stage(stage) if ok else {}
        db_sync.record_run(stage, started_at, datetime.now(timezone.utc), ok)
        if ok:
            logger.info("Database (%s): %s saved %s", db.describe(), stage, counts)
    except Exception as error:
        db.reset()
        logger.error("Database (%s): could not save %s: %s: %s. The JSON files are complete; load them later "
                     "with: python -m src.core.migrate_to_postgres", db.describe(), stage, type(error).__name__, error)


def main():
    args = parse_args()
    stages = [stage for stage in STAGE_ORDER if stage in (args.stages or STAGE_ORDER)]

    run_id = os.environ.get(RUN_ID_ENV) or new_run_id()
    os.environ[RUN_ID_ENV] = run_id
    configure_logging(log_file=f"pipeline_{run_id}.log")
    logger.info("Run %s: stages %s", run_id, ", ".join(stages))

    for stage in stages:
        logger.info("===== Running stage: %s =====", stage)
        started_at = datetime.now(timezone.utc)
        ok = run_stage(stage, **({"since": args.since} if stage == "source_collection" else {}))
        save_to_database(stage, started_at, ok)
        if not ok:
            logger.error("Stopping: stage %s did not complete; later stages were not run.", stage)
            sys.exit(1)

    logger.info("Run %s complete.", run_id)


if __name__ == "__main__":
    main()
