"""Step 3: group the fetched articles into incidents (one crash = one incident).

Input:  data/link_extraction/articles.json (articles with filter_decision "keep")
Output: data/incident_grouping/incidents.json

Grouping is deterministic (case_grouping.py) and runs on full-text facts, across
all articles regardless of the source-collection case they came from. Each incident
keeps every supporting article (text, metadata, URLs), the candidate dates and
locations each article gives, disagreements between articles, and what is still
unknown. No LLM is used here.

Articles accumulate from run to run, so each run groups all of them again (a new article may belong
to an earlier crash). An incident keeps the id it had in the previous incidents.json. A new article that
joins an earlier incident is discarded as a duplicate (listed under "discarded_duplicates") unless it
states something new: a victim's name, an age, the date, the place... Then the incident is unchanged and
the later stages skip it; with new information the incident gains the article and is screened again.
"""

import hashlib
import logging
from collections import Counter

from config.settings import module_data_dir
from src.core.date_utils import iso, utc_now
from src.core.logger import configure_logging
from src.core.outputs import read_json, write_stage_json
from src.modules.link_extraction.full_text_filter import KEEP
from src.modules.source_collection.case_grouping import facts, group_with_reasons, victim_ages, victim_types
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

    if data.get("complete") is False:
        logger.warning("articles.json is from an interrupted link_extraction run; grouping the articles read so far.")

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


def member_key(member):
    return member.get("canonical_url") or member.get("url") or member["article_id"]


def match_earlier(groups, earlier):
    """For each group of articles, the earlier incident it grew out of (the one sharing the most
    articles; an earlier incident is used once), or None for a new crash."""
    owner, by_id = {}, {old["incident_id"]: old for old in earlier}
    for old in earlier:
        for member in old.get("articles") or []:
            owner[member_key(member)] = old["incident_id"]
    taken, matches = set(), []
    for members, _ in groups:
        votes = Counter(owner[k] for k in map(member_key, members) if k in owner)
        old_id = next((i for i, _ in votes.most_common() if i not in taken), None)
        taken.add(old_id)
        matches.append(by_id.get(old_id))
    return matches


def new_information(member):
    """What an article states that could change the lead: victim names, ages, case number, crash date,
    place, county, road, and having readable text at all."""
    f = facts(member)
    info = {("name", n) for n in f["names"]} | {("age", a) for a in f["ages"]}
    for field in ("case_number", "place", "county", "road"):
        if f[field]:
            info.add((field, f[field]))
    if f["date"]:
        info.add(("date", f["date"].date().isoformat() if hasattr(f["date"], "date") else str(f["date"])))
    if member.get("text_available"):
        info.add(("text", True))
    return info


def drop_duplicate_articles(members, old):
    """(members, discarded): articles that join an earlier incident are dropped unless they state
    something the incident did not have yet (a victim's name, age, date, place...). The incident then
    stays exactly as it was and is not screened or verified again."""
    earlier_keys = {member_key(m) for m in old.get("articles") or []}
    kept = [m for m in members if member_key(m) in earlier_keys]
    if not kept:
        return members, []
    known = set().union(*(new_information(m) for m in kept))
    discarded = []
    for member in members:
        if member_key(member) in earlier_keys:
            continue
        fresh = new_information(member) - known
        if fresh:
            known |= fresh
        else:
            discarded.append({
                "article_id": member.get("article_id"), "url": member.get("url"), "title": member.get("title"),
                "incident_id": old["incident_id"], "reason": "adds nothing new to an incident from an earlier run",
            })
    skip = {d["article_id"] for d in discarded}
    return [m for m in members if m.get("article_id") not in skip], discarded


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

    earlier = [i for i in (read_json(module_data_dir(MODULE) / INCIDENTS_FILE, default={}) or {}).get("incidents") or []
               if i.get("incident_id")]
    groups = group_with_reasons(articles)
    incidents, discarded, grown = [], [], 0
    for (members, rationale), old in zip(groups, match_earlier(groups, earlier)):
        if old:
            members, dropped = drop_duplicate_articles(members, old)
            discarded += dropped
        record = incident_record(members, rationale)
        if old:  # same crash as an earlier run: same id, so the later stages recognise it
            grown += record["incident_id"] != old["incident_id"]
            record["incident_id"] = old["incident_id"]
        incidents.append(record)
    logger.info("%s incidents gained articles with new information; %s duplicate articles discarded", grown, len(discarded))
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
            "duplicate_articles_discarded": len(discarded),
        },
        "discarded_duplicates": discarded,
        "incidents": incidents,
    })
    logger.info("Incidents: %s (from %s articles) -> %s", len(incidents), len(articles), path)
    return True


if __name__ == "__main__":
    configure_logging()
    main()
