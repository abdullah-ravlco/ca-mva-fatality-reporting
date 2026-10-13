"""Step 3: group the fetched articles into incidents (one crash = one incident).

Input:  data/link_extraction/articles.json (articles with filter_decision "keep")
Output: data/incident_grouping/incidents.json

Grouping is deterministic (case_grouping.py) and runs on full-text facts, across
all articles regardless of the source-collection case they came from. Each incident
keeps every supporting article (text, metadata, URLs), the candidate dates and
locations each article gives, disagreements between articles, and what is still
unknown. No LLM is used here.
"""

import hashlib
import logging

from config.settings import module_data_dir
from src.core.date_utils import iso, utc_now
from src.core.logger import configure_logging
from src.core.outputs import read_json, write_stage_json
from src.modules.link_extraction.full_text_filter import KEEP
from src.modules.source_collection.case_grouping import group_with_reasons, victim_ages, victim_types
from src.modules.source_collection.location import CALIFORNIA_CONFIRMED

MODULE = "incident_grouping"
INPUT_FILE = module_data_dir("link_extraction") / "articles.json"
INCIDENTS_FILE = "incidents.json"
DATE_TOLERANCE_DAYS = 1

logger = logging.getLogger(__name__)


def load_articles():
    data = read_json(INPUT_FILE)
    if not isinstance(data, dict) or not isinstance(data.get("articles"), list):
        raise FileNotFoundError(f"Articles file missing or invalid: {INPUT_FILE}")

    kept, seen = [], set()
    for article in data["articles"]:
        if article.get("filter_decision") != KEEP or article.get("article_id") in seen:
            continue
        seen.add(article["article_id"])
        kept.append(article)
    return kept, len(data["articles"])


def incident_id(members):
    keys = sorted(m.get("canonical_url") or m.get("url") or m["article_id"] for m in members)
    return "inc-" + hashlib.sha1("|".join(keys).encode()).hexdigest()[:12]


def candidate_dates(members):
    return [
        {
            "date": (m["incident_date"] or "")[:10] or None,
            "basis": m.get("incident_date_basis"),
            "text": m.get("incident_date_text"),
            "evidence": m.get("incident_date_evidence"),
            "article_id": m["article_id"],
        }
        for m in members if m.get("incident_date")
    ]


def locations(members):
    found = []
    for m in members:
        if any(m.get(k) for k in ("county", "place", "road", "location_text")):
            found.append({
                "county": m.get("county"),
                "place": m.get("place"),
                "road": m.get("road"),
                "location_text": m.get("location_text"),
                "location_status": m.get("location_status"),
                "evidence": m.get("location_evidence"),
                "article_id": m["article_id"],
            })
    return found


def conflicts(dates, places):
    notes = []
    distinct_dates = sorted({d["date"] for d in dates if d["date"]})
    if len(distinct_dates) > 1:
        notes.append({
            "field": "incident_date",
            "values": distinct_dates,
            "note": f"articles give dates up to {DATE_TOLERANCE_DAYS} day apart (late-night crashes are often reported both ways)",
        })
    for field in ("county", "place", "road"):
        values = sorted({p[field] for p in places if p[field]})
        if len(values) > 1:
            notes.append({"field": field, "values": values, "note": "articles name different values"})
    return notes


def uncertainties(members, dates):
    notes = []
    if not dates:
        notes.append("no article states a resolvable crash date")
    if not any(m.get("location_status") == CALIFORNIA_CONFIRMED for m in members):
        notes.append("California location not confirmed by any article")
    if not any(m.get("text_available") for m in members):
        notes.append("no article text could be read; evidence is RSS/listing metadata only")
    for m in members:
        if m.get("incident_date_note"):
            notes.append(f"{m['article_id']}: {m['incident_date_note']}")
    return notes


def incident_record(members, rationale):
    dates = candidate_dates(members)
    places = locations(members)
    ages = set()
    types = set()
    for member in members:
        ages |= victim_ages(member)
        types |= victim_types(member)

    return {
        "incident_id": incident_id(members),
        "source_case_ids": sorted({m["case_id"] for m in members if m.get("case_id")}),
        "candidate_incident_dates": dates,
        "locations": places,
        "victim_types_mentioned": sorted(types),
        "victim_ages_mentioned": sorted(ages),
        "article_count": len(members),
        "articles_with_text": sum(1 for m in members if m.get("text_available")),
        "publishers": sorted({m["publisher"] for m in members if m.get("publisher")}),
        "source_urls": [m.get("url") for m in members],
        "conflicts": conflicts(dates, places),
        "uncertainties": uncertainties(members, dates),
        "grouping_rationale": rationale,
        "articles": members,
    }


def main():
    now = utc_now()
    articles, total = load_articles()
    logger.info("Articles kept by the full-text filter: %s of %s", len(articles), total)

    incidents = [incident_record(members, rationale) for members, rationale in group_with_reasons(articles)]
    incidents.sort(key=lambda i: max((d["date"] for d in i["candidate_incident_dates"] if d["date"]), default=""), reverse=True)

    path = write_stage_json(MODULE, INCIDENTS_FILE, {
        "run_at": iso(now),
        "input_file": str(INPUT_FILE),
        "summary": {
            "articles_in": total,
            "articles_kept": len(articles),
            "incidents": len(incidents),
            "multi_article_incidents": sum(1 for i in incidents if i["article_count"] > 1),
            "incidents_without_text": sum(1 for i in incidents if not i["articles_with_text"]),
        },
        "incidents": incidents,
    })
    logger.info("Incidents: %s (from %s articles) -> %s", len(incidents), len(articles), path)
    return True


if __name__ == "__main__":
    configure_logging()
    main()
