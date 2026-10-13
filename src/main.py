"""Application CLI entrypoint & orchestrator.

Runs the pipeline stages in order:
  1. source_collection  - find candidate crash records from official/RSS sources
  2. link_extraction    - fetch and extract article text for each candidate
  3. article_analysis   - LLM extraction + shortlist of named people
  4. lead_verification  - verify the shortlisted leads against sources

Usage:
  python -m src.main                 # run every stage, in order
  python -m src.main --stage STAGE   # run one stage (repeatable)
"""

import argparse

from src.modules.article_analysis import pipeline as article_analysis
from src.modules.lead_verification import pipeline as lead_verification
from src.modules.link_extraction import pipeline as link_extraction
from src.modules.source_collection import pipeline as source_collection

STAGES = {
    "source_collection": source_collection.main,
    "link_extraction": link_extraction.main,
    "article_analysis": article_analysis.main,
    "lead_verification": lead_verification.main,
}

STAGE_ORDER = list(STAGES)


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


def main():
    args = parse_args()
    stages = args.stages or STAGE_ORDER

    for stage in stages:
        print(f"\n===== Running stage: {stage} =====")
        STAGES[stage]()


if __name__ == "__main__":
    main()
