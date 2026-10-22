"""Dagster definitions: the six pipeline stages as assets, so they can be run, scheduled and
watched from the Dagster UI.

Each asset calls the stage's existing main(); the stage code is unchanged and still writes to
data/<stage>/ and data/runs/<run_id>/<stage>/. src/main.py keeps working as before.

Groups:
  collection - stages 1-3, free (no LLM, no paid API); scheduled daily (off until switched on)
  ai_and_paid - stages 4-6, call JEV, OpenAI and SearchBug; run by hand only

Usage (from the repository root):
  DAGSTER_HOME=$PWD/.dagster_home .venv/bin/dagster dev -m src.orchestration.definitions
"""

import os
from datetime import datetime, timezone

import dagster as dg

from config.settings import DATA_DIR, module_data_dir
from src.core.logger import configure_logging
from src.core.outputs import RUN_ID_ENV, read_json
from src.modules.contact_enrichment import config as contact_config
from src.modules.contact_enrichment import pipeline as contact_enrichment
from src.modules.incident_grouping import pipeline as incident_grouping
from src.modules.lead_verification import config as lead_config
from src.modules.lead_verification import pipeline as lead_verification
from src.modules.link_extraction import pipeline as link_extraction
from src.modules.severity_screening import config as screening_config
from src.modules.severity_screening import pipeline as severity_screening
from src.modules.source_collection import pipeline as source_collection

COLLECTION = "collection"
AI_AND_PAID = "ai_and_paid"

# An incident with more articles than this is probably two crashes grouped together.
MAX_ARTICLES_PER_INCIDENT = 15


def pipeline_run_id(context):
    """One data/runs/<run_id>/ folder per Dagster run, named like src/main.py's (time first, so
    folders sort by date), plus the start of the Dagster run id to find it in the UI."""
    record = context.instance.get_run_record_by_id(context.run_id)
    started = datetime.fromtimestamp(record.create_timestamp.timestamp(), timezone.utc)
    return f"{started:%Y%m%dT%H%M%SZ}-{context.run_id[:8]}"


def run_stage(context, name, stage_main):
    """Run one stage's main() with the run id and log file src/main.py would use; a stage
    that returns False or raises fails the asset, so later stages do not run."""
    run_id = pipeline_run_id(context)
    os.environ[RUN_ID_ENV] = run_id
    configure_logging(log_file=f"pipeline_{run_id}.log")
    context.log.info("Stage %s, run %s", name, run_id)
    if stage_main() is False:
        raise dg.Failure(description=f"Stage {name} did not complete; see logs/pipeline_{run_id}.log")
    return run_id


def summary_metadata(run_id, path, items_key):
    """Shown next to the asset in the UI: the run id, the output file, how many items it holds,
    and the stage's own summary numbers (charted across runs); a by_* breakdown such as
    by_fetch_status becomes one number per value, e.g. by_fetch_status.ok."""
    data = read_json(path, default={}) or {}
    metadata = {"run_id": run_id, "output_file": dg.MetadataValue.path(str(path))}
    if isinstance(data.get(items_key), list):
        metadata[items_key] = len(data[items_key])
    for key, value in (data.get("summary") or {}).items():
        if is_number(value):
            metadata[key] = value
        elif key.startswith("by_") and isinstance(value, dict):
            metadata.update({f"{key}.{name}": count for name, count in value.items() if is_number(count)})
    return metadata


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------- stages 1-3: free

@dg.asset(group_name=COLLECTION, description="Stage 1: collect RSS, Google News and Tavily items, "
          "then filter them into finalized_raw.json.")
def finalized_raw(context: dg.AssetExecutionContext):
    run_id = run_stage(context, "source_collection", source_collection.main)
    path = module_data_dir("source_collection") / "finalized_raw.json"
    return dg.MaterializeResult(metadata=summary_metadata(run_id, path, "items"))


@dg.asset(deps=[finalized_raw], group_name=COLLECTION, description="Stage 2: open every link, read the "
          "full text and re-check date, location and topic.")
def articles(context: dg.AssetExecutionContext):
    run_id = run_stage(context, "link_extraction", link_extraction.main)
    path = module_data_dir("link_extraction") / "articles.json"
    return dg.MaterializeResult(metadata=summary_metadata(run_id, path, "articles"))


@dg.asset(deps=[articles], group_name=COLLECTION, description="Stage 3: group the kept articles "
          "into incidents (no LLM).")
def incidents(context: dg.AssetExecutionContext):
    run_id = run_stage(context, "incident_grouping", incident_grouping.main)
    path = module_data_dir("incident_grouping") / "incidents.json"
    return dg.MaterializeResult(metadata=summary_metadata(run_id, path, "incidents"))


# ---------------------------------------------------------------- stages 4-6: AI and paid

@dg.asset(deps=[incidents], group_name=AI_AND_PAID, description="Stage 4: JEV screens each incident; "
          "only catastrophic injuries caused by another party go on. Calls the JEV API.")
def screened_incidents(context: dg.AssetExecutionContext):
    run_id = run_stage(context, "severity_screening", severity_screening.main)
    return dg.MaterializeResult(metadata=summary_metadata(run_id, screening_config.OUTPUT_FILE, "incidents"))


@dg.asset(deps=[screened_incidents], group_name=AI_AND_PAID, description="Stage 5: an OpenAI agent "
          "investigates each lead that passed screening. Calls OpenAI.")
def verified_leads(context: dg.AssetExecutionContext):
    run_id = run_stage(context, "lead_verification", lead_verification.main)
    return dg.MaterializeResult(metadata=summary_metadata(run_id, lead_config.OUTPUT_FILE, "leads"))


@dg.asset(deps=[verified_leads], group_name=AI_AND_PAID, description="Stage 6: SearchBug lookups for "
          "the deceased and their next of kin. PAID, and handles personal data.")
def contacts(context: dg.AssetExecutionContext):
    run_id = run_stage(context, "contact_enrichment", contact_enrichment.main)
    return dg.MaterializeResult(metadata=summary_metadata(run_id, contact_config.OUTPUT_FILE, "people"))


# ---------------------------------------------------------------- checks

def places(incident):
    """The distinct places an incident's articles name, e.g. Fairfield, Emeryville."""
    found = [loc.get("place") or loc.get("county") for loc in incident.get("locations") or []]
    return sorted({name for name in found if name})


@dg.asset_check(asset=finalized_raw, description="Collection found at least one item.")
def finalized_raw_not_empty():
    data = read_json(module_data_dir("source_collection") / "finalized_raw.json", default={}) or {}
    count = len(data.get("items") or [])
    return dg.AssetCheckResult(passed=count > 0, metadata={"items": count})


@dg.asset_check(asset=incidents, description=f"No incident has more than {MAX_ARTICLES_PER_INCIDENT} "
                "articles (a sign two crashes were grouped together). A warning, not a stop.")
def incidents_not_over_merged():
    data = read_json(module_data_dir("incident_grouping") / "incidents.json", default={}) or {}
    large = [i for i in data.get("incidents") or [] if (i.get("article_count") or 0) > MAX_ARTICLES_PER_INCIDENT]
    return dg.AssetCheckResult(
        passed=not large,
        severity=dg.AssetCheckSeverity.WARN,
        metadata={
            "over_merged": len(large),
            "largest": dg.MetadataValue.md("\n".join(
                f"- {i['incident_id']}: {i['article_count']} articles, {', '.join(places(i))}"
                for i in sorted(large, key=lambda i: -i["article_count"])[:10]) or "none"),
        },
    )


# ---------------------------------------------------------------- jobs + schedule

collection_job = dg.define_asset_job(
    "collection_job", selection=dg.AssetSelection.groups(COLLECTION),
    description="Stages 1-3: collect, extract and group. Free.",
)

ai_and_paid_job = dg.define_asset_job(
    "ai_and_paid_job", selection=dg.AssetSelection.groups(AI_AND_PAID),
    description="Stages 4-6: JEV, OpenAI and SearchBug. Costs money; run by hand.",
)

daily_collection = dg.ScheduleDefinition(
    job=collection_job,
    cron_schedule="0 6 * * *",
    execution_timezone="America/Los_Angeles",
    default_status=dg.DefaultScheduleStatus.STOPPED,  # off until switched on in the UI
)

defs = dg.Definitions(
    assets=[finalized_raw, articles, incidents, screened_incidents, verified_leads, contacts],
    asset_checks=[finalized_raw_not_empty, incidents_not_over_merged],
    jobs=[collection_job, ai_and_paid_job],
    schedules=[daily_collection],
)
