
import json
import logging
import os
import time
from datetime import datetime, timezone

from config.settings import module_data_dir
from src.core.logger import configure_logging
from src.modules.article_analysis.llm_extractor import analyze_article, build_llm


# --------------------------------------------------
# Configuration
# --------------------------------------------------

INPUT_FILE = module_data_dir("link_extraction") / "extracted_text.json"

# Step 1 output: gives each article's publication date (not in extracted_text.json).
SOURCES_FILE = module_data_dir("source_collection") / "sources_to_open.json"

OUTPUT_DIR = module_data_dir("article_analysis")

ALL_ARTICLES_FILE = OUTPUT_DIR / "all_articles.json"
SHORTLIST_FILE = OUTPUT_DIR / "shortlisted_articles.json"

DELAY_SECONDS = 8

# Articles that fail are retried in the same run (after the others), so one
# bad LLM reply does not need a manual re-run of the pipeline.
RETRY_PASSES = 2
RETRY_WAIT_SECONDS = 30

TEXT_KEYS = (
    "extracted_text",
    "article_text",
    "text",
    "content",
    "full_text",
)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

configure_logging(log_file="article_analysis.log")

logger = logging.getLogger(__name__)


# --------------------------------------------------
# General helpers
# --------------------------------------------------

def current_timestamp():
    return datetime.now(timezone.utc).isoformat()


def write_json_atomic(path, data):
    """
    Write JSON to a temporary file, then replace the destination.
    This reduces the risk of leaving an empty/partial JSON file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = path.with_suffix(path.suffix + ".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())

    os.replace(temporary_path, path)


def read_json(path):
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


# --------------------------------------------------
# Input normalization
# --------------------------------------------------

def get_article_text(record):
    for key in TEXT_KEYS:
        value = record.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def normalize_article(record):
    """
    Standardize input fields while preserving the original fields.
    """
    article = dict(record)

    article["article_title"] = (
        article.get("article_title")
        or article.get("title")
        or article.get("headline")
        or ""
    )

    article["source_url"] = (
        article.get("source_url")
        or article.get("url")
        or article.get("link")
        or article.get("final_url")
        or ""
    )

    article["extracted_text"] = get_article_text(record)

    return article


def find_articles(data):
    """
    Support a JSON list of articles or a dictionary containing
    an articles/records/results list.
    """
    if isinstance(data, list):
        records = data

    elif isinstance(data, dict):
        records = None

        for key in ("articles", "records", "results", "items"):
            if isinstance(data.get(key), list):
                records = data[key]
                break

        if records is None:
            raise ValueError(
                "Could not find an article list in the input JSON. "
                "Expected a list or a dictionary containing "
                "'articles', 'records', 'results', or 'items'."
            )
    else:
        raise ValueError("The input JSON must be a list or dictionary.")

    articles = []

    for record in records:
        if not isinstance(record, dict):
            continue

        article = normalize_article(record)

        if not article["source_url"]:
            logger.warning(
                "Skipping an article without a URL: %s",
                article["article_title"] or "(no title)",
            )
            continue

        if not article["extracted_text"]:
            logger.warning(
                "Skipping an article without extracted text: %s",
                article["article_title"] or article["source_url"],
            )
            continue

        articles.append(article)

    return articles


def load_publication_dates():
    """(by_url, by_case_id) publication dates from sources_to_open.json."""
    by_url, by_case = {}, {}
    if not SOURCES_FILE.exists():
        return by_url, by_case

    try:
        data = read_json(SOURCES_FILE)
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read %s; publication dates unavailable.", SOURCES_FILE)
        return by_url, by_case

    for category in ("confirmed", "doubtful"):
        for case in data.get(category) or []:
            for source in case.get("sources") or []:
                published = source.get("published_at")
                if not published:
                    continue
                if source.get("url"):
                    by_url[source["url"]] = published
                case_id = case.get("case_id")
                if case_id and (case_id not in by_case or published < by_case[case_id]):
                    by_case[case_id] = published

    return by_url, by_case


def add_pipeline_context(articles):
    """Publication date and the earlier step's incident date for each article.

    The LLM needs the publication date to resolve "Friday" or "Oct. 8"; the
    shortlist uses both when the article itself gives no full date.
    """
    by_url, by_case = load_publication_dates()

    for article in articles:
        article["published_at"] = (
            article.get("published_at")
            or by_url.get(article.get("source_url"))
            or by_url.get(article.get("link"))
            or by_case.get(article.get("case_id"))
        )
        article["pipeline_incident_date"] = article.get("incident_date")


CONTEXT_FIELDS = ("published_at", "pipeline_incident_date")


def copy_context(article, record):
    """Copy pipeline context into an analysis record (also fills old records)."""
    record["source_url"] = article.get("source_url") or record.get("source_url")
    for field in CONTEXT_FIELDS:
        if record.get(field) is None:
            record[field] = article.get(field)


def article_key(article):
    """
    Use the source URL as the stable identifier for resume processing.
    """
    return (
        article.get("source_url")
        or article.get("url")
        or article.get("case_id")
        or article.get("article_title")
        or article.get("title")
    )


# --------------------------------------------------
# Previous results and resume support
# --------------------------------------------------

def load_previous_results():
    """
    Preserve successful results from the previous run.

    Previous failures are retried instead of being treated as completed.
    """
    if not ALL_ARTICLES_FILE.exists():
        return {}

    try:
        previous_data = read_json(ALL_ARTICLES_FILE)

    except (json.JSONDecodeError, OSError) as error:
        logger.warning(
            "Previous results could not be loaded: %s. "
            "Starting without resume data.",
            error,
        )
        logger.warning(
            "If the file contains important data, preserve a copy "
            "before overwriting it."
        )
        return {}

    previous_articles = previous_data.get("articles", [])

    if not isinstance(previous_articles, list):
        logger.warning(
            "Previous results have an unexpected format. "
            "Starting without resume data."
        )
        return {}

    successful = {}

    for record in previous_articles:
        if not isinstance(record, dict):
            continue

        if record.get("analysis_status") != "success":
            continue

        # Results from before outcome_category/caused_crash existed are
        # re-analysed so injury severity and who caused the crash are known.
        if any(
            "outcome_category" not in person
            for person in record.get("people") or []
        ):
            continue

        key = article_key(record)

        if key:
            successful[key] = record

    return successful


# --------------------------------------------------
# Shortlist generation
# --------------------------------------------------

def build_shortlist_from_results(records):
    """
    (shortlisted, skipped) people, decided by shortlist.py:
      - a named person who died or was seriously injured (age optional)
        and did not cause the crash,
      - incident date within 10 days, or unknown (kept for review),
      - location not explicitly outside California,
    """
    try:
        from src.modules.article_analysis.shortlist import build_shortlist_report
    except ImportError:
        logger.error(
            "Could not import build_shortlist_report from shortlist.py. "
            "The shortlist file will contain no records."
        )
        return [], []

    article_records = [
        record
        for record in records
        if record.get("analysis_status") == "success"
    ]

    return build_shortlist_report(article_records)


# --------------------------------------------------
# Save progress
# --------------------------------------------------

def save_progress(results, total_input):
    records = list(results.values())

    successful = [
        record
        for record in records
        if record.get("analysis_status") == "success"
    ]

    failed = [
        record
        for record in records
        if record.get("analysis_status") == "failed"
    ]

    output = {
        "run_at": current_timestamp(),
        "input_file": str(INPUT_FILE),
        "total_articles_found": total_input,
        "total_articles_analyzed": len(successful),
        "total_articles_failed": len(failed),
        "articles": records,
    }

    write_json_atomic(ALL_ARTICLES_FILE, output)

    shortlisted_people, skipped_people = build_shortlist_from_results(successful)

    shortlist_output = {
        "run_at": current_timestamp(),
        "selection_rule": (
            "Named person who died or was seriously injured and did not "
            "cause the crash (age optional); incident date within 10 days "
            "(within 6 is 'recent') or unknown; location not outside "
            "California. "
            "Verify each record against its original source."
        ),
        "total_shortlisted_people": len(shortlisted_people),
        "people": shortlisted_people,
        "total_skipped_named_people": len(skipped_people),
        "skipped_named_people": skipped_people,
    }

    write_json_atomic(SHORTLIST_FILE, shortlist_output)

    logger.info(
        "Progress saved. Successful: %s | Failed: %s",
        len(successful),
        len(failed),
    )


def analyze_one(article, client):
    """Analysis record for one article; a failed record instead of raising."""
    title = article.get("article_title") or article["source_url"]

    try:
        # analyze_article returns an ArticleAnalysis Pydantic object.
        analysis_object = analyze_article(article, client)

        # Convert it to a dictionary BEFORE assigning extra fields.
        if hasattr(analysis_object, "model_dump"):
            analysis = analysis_object.model_dump()
        elif isinstance(analysis_object, dict):
            analysis = dict(analysis_object)
        else:
            raise TypeError(
                "analyze_article must return a Pydantic model "
                "or a dictionary."
            )

        # Preserve identifiers from the source record.
        analysis["case_id"] = article.get("case_id")
        analysis["analysis_status"] = "success"
        copy_context(article, analysis)

        # Preserve useful source metadata if present.
        analysis["source_name"] = (
            article.get("source_name")
            or article.get("publisher")
        )

        return analysis

    except Exception as error:
        logger.exception("Failed to analyze: %s", title)

        return {
            "article_title": title,
            "source_url": article.get("source_url"),
            "case_id": article.get("case_id"),
            "analysis_status": "failed",
            "error": str(error),
        }


def run_pass(batch, client, results, total_input, label):
    """Analyse each article in batch, saving after each one.

    Returns False if progress could not be saved (the run should stop).
    """
    for index, article in enumerate(batch, start=1):
        title = article.get("article_title") or article["source_url"]

        logger.info("%s article %s/%s: %s", label, index, len(batch), title)

        results[article_key(article)] = analyze_one(article, client)

        # Save after every article so progress is not lost between calls.
        try:
            save_progress(results, total_input)
        except Exception:
            logger.exception("Could not save progress.")
            # Stop rather than continue if checkpointing fails.
            return False

        if index < len(batch):
            time.sleep(DELAY_SECONDS)

    return True


# --------------------------------------------------
# Main processing
# --------------------------------------------------

def main():
    if not INPUT_FILE.exists():
        logger.error("Input file does not exist: %s", INPUT_FILE)
        return

    try:
        input_data = read_json(INPUT_FILE)
        articles = find_articles(input_data)
    except (json.JSONDecodeError, OSError, ValueError) as error:
        logger.exception("Could not read input articles: %s", error)
        return

    logger.info("Found %s input articles.", len(articles))
    add_pipeline_context(articles)

    previous_results = load_previous_results()

    # Only successful previous results count as completed.
    results = dict(previous_results)

    # Earlier results get publication dates without being re-analysed.
    for article in articles:
        previous = results.get(article_key(article))
        if previous:
            copy_context(article, previous)

    pending = [
        article
        for article in articles
        if article_key(article) not in results
    ]

    logger.info(
        "%s articles already successful; %s remaining.",
        len(results),
        len(pending),
    )

    if not pending:
        save_progress(results, len(articles))
        logger.info("All articles have already been analyzed successfully.")
        return

    try:
        client = build_llm()
    except Exception:
        logger.exception("Could not initialize the OpenAI client.")
        return

    if not run_pass(pending, client, results, len(articles), "Processing"):
        return

    for retry in range(1, RETRY_PASSES + 1):
        failed = [
            article
            for article in pending
            if results.get(article_key(article), {}).get("analysis_status") == "failed"
        ]
        if not failed:
            break

        logger.info(
            "Retry pass %s/%s for %s failed article(s) in %ss.",
            retry,
            RETRY_PASSES,
            len(failed),
            RETRY_WAIT_SECONDS,
        )
        time.sleep(RETRY_WAIT_SECONDS)

        if not run_pass(failed, client, results, len(articles), f"Retry {retry}"):
            return

    logger.info("Analysis run finished.")
    logger.info("Full results: %s", ALL_ARTICLES_FILE)
    logger.info("Shortlist: %s", SHORTLIST_FILE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.warning(
            "Interrupted by user. Previously saved progress is retained."
        )
