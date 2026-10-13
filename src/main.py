"""Application CLI entrypoint & orchestrator.

Runs the pipeline stages in order:
  1. source_collection  - discover articles (Google News RSS, publisher RSS, coroner/CHP
                          pages), filter them, remove duplicate articles
  2. link_extraction    - fetch every article, read text + page metadata, re-check
                          crash date / location / relevance on the full text
  3. incident_grouping  - group the kept articles into incidents (no LLM)
  4. lead_verification  - one LLM analysis per incident, quote-checked against sources

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
from src.modules.incident_grouping import pipeline as incident_grouping
from src.modules.lead_verification import pipeline as lead_verification
from src.modules.link_extraction import pipeline as link_extraction
from src.modules.source_collection import pipeline as source_collection

STAGES = {
    "source_collection": source_collection.main,
    "link_extraction": link_extraction.main,
    "incident_grouping": incident_grouping.main,
    "lead_verification": lead_verification.main,
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
