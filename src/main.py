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
"""

import argparse
import logging
import os
import sys

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
    return parser.parse_args()


def run_stage(stage):
    """True when the stage finished successfully."""
    try:
        return STAGES[stage]() is not False
    except Exception:
        logger.exception("Stage %s failed", stage)
        return False


def main():
    args = parse_args()
    stages = [stage for stage in STAGE_ORDER if stage in (args.stages or STAGE_ORDER)]

    run_id = os.environ.get(RUN_ID_ENV) or new_run_id()
    os.environ[RUN_ID_ENV] = run_id
    configure_logging(log_file=f"pipeline_{run_id}.log")
    logger.info("Run %s: stages %s", run_id, ", ".join(stages))

    for stage in stages:
        logger.info("===== Running stage: %s =====", stage)
        if not run_stage(stage):
            logger.error("Stopping: stage %s did not complete; later stages were not run.", stage)
            sys.exit(1)

    logger.info("Run %s complete.", run_id)


if __name__ == "__main__":
    main()
