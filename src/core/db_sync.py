"""Load each stage's JSON output into the Postgres tables (see db_migrations/).

The JSON files stay the source of truth: after a stage finishes, src/main.py calls sync_stage(stage),
which reads that stage's output file and writes it into the tables in one transaction. Every load is
an upsert, so running it again (or loading old files with migrate_to_postgres) never duplicates rows.

  source_collection   finalized_raw.json         -> collected_items
  link_extraction     articles.json              -> publishers, articles
  incident_grouping   incidents.json             -> incidents (grouping), articles.incident_id
  severity_screening  screened_incidents.json    -> incidents (screening)
  lead_verification   verified_leads_details.json-> incidents (verification), people, evidence
  contact_enrichment  contacts.json (+ details)  -> people (SearchBug details), next_of_kin, contact_points
"""

import logging
import re
from datetime import date

from config.settings import module_data_dir
from src.core import db
from src.core.date_utils import parse_date
from src.core.outputs import read_json, run_id

logger = logging.getLogger(__name__)

FILES = {
    "source_collection": module_data_dir("source_collection") / "finalized_raw.json",
    "link_extraction": module_data_dir("link_extraction") / "articles.json",
    "incident_grouping": module_data_dir("incident_grouping") / "incidents.json",
    "severity_screening": module_data_dir("severity_screening") / "screened_incidents.json",
    "lead_verification": module_data_dir("lead_verification") / "verified_leads_details.json",
    "contact_enrichment": module_data_dir("contact_enrichment") / "contacts.json",
}
CONTACT_DETAILS_FILE = module_data_dir("contact_enrichment") / "contacts_details.json"
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


# --------------------------------------------------
# Value helpers
# --------------------------------------------------

def to_timestamp(value):
    return parse_date(value) if value else None


def to_date(value):
    """A full YYYY-MM-DD date, or None (partial dates such as '1959' are not stored as dates)."""
    text = str(value or "").strip()
    if not ISO_DATE_RE.match(text):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def to_int(value):
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() else None


def fact(node):
    """The value of a {"value", "quote", ...} fact (or the value itself)."""
    return node.get("value") if isinstance(node, dict) else node


def person_key(name, index):
    """How a person is recognized across stages: their normalized name, or 'unnamed-N'."""
    words = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    return " ".join(words) if words else f"unnamed-{index}"


def split_name(name):
    words = (name or "").split()
    while len(words) > 2 and words[-1].lower() in SUFFIXES:
        words = words[:-1]
    if len(words) < 2:
        return (words[0] if words else None), None
    return words[0], words[-1]


def _list(value):
    return [str(v) for v in value] if isinstance(value, list) else None


def _load(stage):
    data = read_json(FILES[stage], default=None)
    if data is None:
        logger.info("Database: no %s output to load (%s)", stage, FILES[stage])
    return data


# --------------------------------------------------
# Rows shared by several stages
# --------------------------------------------------

def ensure_incident(cur, incident_id):
    cur.execute("INSERT INTO incidents (incident_id) VALUES (%s) ON CONFLICT (incident_id) DO NOTHING", (incident_id,))


def publisher_id(cur, name, cache):
    name = (name or "").strip()
    if not name:
        return None
    if name not in cache:
        cur.execute(
            "INSERT INTO publishers (name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING publisher_id",
            (name,),
        )
        cache[name] = cur.fetchone()[0]
    return cache[name]


ARTICLE_FIELDS = [
    "incident_id", "publisher_id", "url", "canonical_url", "final_url", "title", "description", "published_at",
    "discovered_by_sources", "discovered_by_queries", "fetch_status", "extraction_method", "extraction_error",
    "text_available", "text", "filter_decision", "filter_reasons", "metadata",
]


def upsert_article(cur, article, publishers, incident_id=None):
    """Insert or update one article; values already stored are kept when the new ones are empty."""
    if not article.get("article_id") or not article.get("url"):
        return 0
    values = {
        "incident_id": incident_id,
        "publisher_id": publisher_id(cur, article.get("publisher"), publishers),
        "url": article.get("url"),
        "canonical_url": article.get("canonical_url"),
        "final_url": article.get("final_url"),
        "title": article.get("title"),
        "description": article.get("description"),
        "published_at": to_timestamp(article.get("published_at")),
        "discovered_by_sources": _list(article.get("discovered_by_sources")),
        "discovered_by_queries": _list(article.get("discovered_by_queries")),
        "fetch_status": article.get("fetch_status"),
        "extraction_method": article.get("extraction_method"),
        "extraction_error": article.get("error"),
        "text_available": article.get("text_available"),
        "text": article.get("text"),
        "filter_decision": article.get("filter_decision"),
        "filter_reasons": _list(article.get("filter_reasons")),
        "metadata": db.jsonb(article.get("metadata")) if article.get("metadata") else None,
    }
    columns = ", ".join(["article_id"] + ARTICLE_FIELDS)
    placeholders = ", ".join(["%s"] * (len(ARTICLE_FIELDS) + 1))
    updates = ", ".join(f"{f} = COALESCE(EXCLUDED.{f}, articles.{f})" for f in ARTICLE_FIELDS)
    cur.execute(
        f"INSERT INTO articles ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT (article_id) DO UPDATE SET {updates}, updated_at = now()",
        [article["article_id"]] + [values[f] for f in ARTICLE_FIELDS],
    )
    return 1


# --------------------------------------------------
# One loader per stage
# --------------------------------------------------

def sync_source_collection(cur):
    data = _load("source_collection")
    if not data:
        return {}
    run = run_id()
    rows = [
        (item["url"], item.get("title"), item.get("description"), item.get("source_name"),
         to_timestamp(item.get("published_date")), run, run)
        for item in data.get("items") or [] if item.get("url")
    ]
    cur.executemany(
        "INSERT INTO collected_items (url, title, description, source_name, published_at, first_seen_run, last_seen_run) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (url) DO UPDATE SET "
        "title = COALESCE(EXCLUDED.title, collected_items.title), "
        "description = COALESCE(EXCLUDED.description, collected_items.description), "
        "source_name = COALESCE(EXCLUDED.source_name, collected_items.source_name), "
        "published_at = COALESCE(EXCLUDED.published_at, collected_items.published_at), "
        "last_seen_run = EXCLUDED.last_seen_run, last_seen_at = now()",
        rows,
    )
    return {"collected_items": len(rows)}


def sync_link_extraction(cur):
    data = _load("link_extraction")
    if not data:
        return {}
    publishers = {}
    count = sum(upsert_article(cur, article, publishers) for article in data.get("articles") or [])
    return {"articles": count}


def sync_incident_grouping(cur):
    data = _load("incident_grouping")
    if not data:
        return {}
    grouped_at = to_timestamp(data.get("run_at"))
    publishers, incidents, articles = {}, 0, 0
    for incident in data.get("incidents") or []:
        cur.execute(
            "INSERT INTO incidents (incident_id, article_count, candidate_dates, grouping_locations, grouping_conflicts, "
            "grouping_rationale, grouped_at) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (incident_id) DO UPDATE SET "
            "article_count = EXCLUDED.article_count, candidate_dates = EXCLUDED.candidate_dates, "
            "grouping_locations = EXCLUDED.grouping_locations, grouping_conflicts = EXCLUDED.grouping_conflicts, "
            "grouping_rationale = EXCLUDED.grouping_rationale, grouped_at = EXCLUDED.grouped_at, updated_at = now()",
            (incident["incident_id"], incident.get("article_count"), db.jsonb(incident.get("candidate_incident_dates") or []),
             db.jsonb(incident.get("locations") or []), db.jsonb(incident.get("conflicts") or []),
             db.jsonb(incident.get("grouping_rationale") or []), grouped_at),
        )
        incidents += 1
        for article in incident.get("articles") or []:
            articles += upsert_article(cur, article, publishers, incident["incident_id"])
    return {"incidents": incidents, "articles": articles}


def sync_severity_screening(cur):
    data = _load("severity_screening")
    if not data:
        return {}
    count = 0
    for record in data.get("incidents") or []:
        ensure_incident(cur, record["incident_id"])
        drop = record.get("drop_reason") or {}
        cur.execute(
            "UPDATE incidents SET screening_status = %s, screening_tags = %s, screening_reasons = %s, "
            "drop_reason_codes = %s, drop_reason = %s, screening_facts = %s, screened_at = %s, updated_at = now() "
            "WHERE incident_id = %s",
            (record.get("status"), _list(record.get("tags")), _list(record.get("reasons")), _list(drop.get("codes")),
             drop.get("text"), db.jsonb(record.get("facts")) if record.get("facts") else None,
             to_timestamp(record.get("screened_at")), record["incident_id"]),
        )
        count += 1
    return {"incidents_screened": count}


PERSON_LEAD_FIELDS = ["full_name", "first_name", "last_name", "age", "role", "outcome", "vehicle", "at_fault",
                      "fault_basis", "fault_explanation", "impaired", "residence"]


def upsert_person(cur, incident_id, key, values):
    """Insert or update a person by (incident, key); empty new values keep what is stored. Returns person_id."""
    fields = list(values)
    columns = ", ".join(["incident_id", "person_key"] + fields)
    placeholders = ", ".join(["%s"] * (len(fields) + 2))
    updates = ", ".join(f"{f} = COALESCE(EXCLUDED.{f}, people.{f})" for f in fields)
    cur.execute(
        f"INSERT INTO people ({columns}) VALUES ({placeholders}) ON CONFLICT (incident_id, person_key) "
        f"DO UPDATE SET {updates}, updated_at = now() RETURNING person_id",
        [incident_id, key] + [values[f] for f in fields],
    )
    return cur.fetchone()[0]


def add_evidence(cur, incident_id, person, field, value, quote, url, verified):
    """One evidence row; a fact that is neither known nor quoted ("unknown", no quote) is not stored."""
    if not quote and (value is None or str(value).strip().lower() in ("", "unknown", "none")):
        return 0
    cur.execute(
        "INSERT INTO evidence (incident_id, person_id, stage, field, value, quote, source_url, quote_verified) "
        "VALUES (%s, %s, 'lead_verification', %s, %s, %s, %s, %s)",
        (incident_id, person, field, None if value is None else str(value), quote, url, verified),
    )
    return 1


def sync_lead_verification(cur):
    data = _load("lead_verification")
    if not data:
        return {}
    counts = {"incidents_verified": 0, "people": 0, "evidence": 0}
    for record in data.get("incidents") or []:
        incident_id = record["incident_id"]
        ensure_incident(cur, incident_id)
        qualification = record.get("qualification") or {}
        facts = record.get("incident") or {}
        cur.execute(
            "UPDATE incidents SET verification_verdict = %s, verification_reasons = %s, verification_explanation = %s, "
            "corrections = %s, missing_information = %s, crash_date = COALESCE(%s, crash_date), "
            "crash_time = COALESCE(%s, crash_time), crash_location = COALESCE(%s, crash_location), "
            "city = COALESCE(%s, city), county = COALESCE(%s, county), road = COALESCE(%s, road), crash_type = %s, "
            "other_party_involved = %s, other_party_description = %s, other_party_at_fault = %s, summary = %s, "
            "verified_at = %s, updated_at = now() WHERE incident_id = %s",
            (qualification.get("verdict"), _list(qualification.get("reasons")), qualification.get("explanation"),
             _list(qualification.get("corrections")), _list(record.get("missing_information")),
             to_date(fact(facts.get("date"))), fact(facts.get("time")), fact(facts.get("location")),
             facts.get("city"), facts.get("county"), facts.get("road"), facts.get("crash_type"),
             facts.get("other_party_involved"), facts.get("other_party_description"), facts.get("other_party_at_fault"),
             facts.get("summary"), to_timestamp(record.get("analysed_at")), incident_id),
        )
        counts["incidents_verified"] += 1
        if record.get("analysis_status") != "analysed":
            continue

        cur.execute("DELETE FROM evidence WHERE incident_id = %s AND stage = 'lead_verification'", (incident_id,))
        for name in ("date", "time", "location"):
            node = facts.get(name) or {}
            counts["evidence"] += add_evidence(cur, incident_id, None, f"crash_{name}", node.get("value"), node.get("quote"),
                                               node.get("source_url"), node.get("quote_verified"))
        counts["evidence"] += add_evidence(
            cur, incident_id, None, "other_party_at_fault", facts.get("other_party_at_fault"),
            facts.get("other_party_fault_quote"), facts.get("other_party_fault_source_url"), None)

        keys = []
        for index, person in enumerate(record.get("people") or [], start=1):
            name = fact(person.get("name"))
            key = person_key(name, index)
            keys.append(key)
            first, last = split_name(name)
            person_id = upsert_person(cur, incident_id, key, {
                "full_name": name, "first_name": first, "last_name": last, "age": to_int(fact(person.get("age"))),
                "role": person.get("role"), "outcome": person.get("outcome"), "vehicle": person.get("vehicle"),
                "at_fault": person.get("at_fault"), "fault_basis": person.get("fault_basis"),
                "fault_explanation": person.get("fault_explanation"), "impaired": person.get("impaired"),
                "residence": fact(person.get("address")),
            })
            counts["people"] += 1
            for field in ("name", "age", "address"):
                node = person.get(field) or {}
                counts["evidence"] += add_evidence(cur, incident_id, person_id, field, node.get("value"), node.get("quote"),
                                                   node.get("source_url"), node.get("quote_verified"))
            for field in ("fault", "impaired"):
                counts["evidence"] += add_evidence(
                    cur, incident_id, person_id, field, person.get("at_fault" if field == "fault" else "impaired"),
                    person.get(f"{field}_quote"), person.get(f"{field}_source_url"), person.get(f"{field}_quote_verified"))
        # People the latest analysis no longer lists (and that were never enriched) are removed.
        cur.execute(
            "DELETE FROM people WHERE incident_id = %s AND match_status IS NULL AND NOT (person_key = ANY(%s))",
            (incident_id, keys),
        )
    return counts


def _contact_rows(owner_column, owner_id, phones, emails):
    rows = [(owner_id, "phone", p["number"], p.get("type"), to_date(p.get("last_seen")), rank)
            for rank, p in enumerate(phones or [], start=1) if p.get("number")]
    rows += [(owner_id, "email", e, None, None, rank) for rank, e in enumerate(emails or [], start=1) if e]
    return rows


def insert_contacts(cur, owner_column, owner_id, phones, emails):
    rows = _contact_rows(owner_column, owner_id, phones, emails)
    if rows:
        cur.executemany(
            f"INSERT INTO contact_points ({owner_column}, kind, value, phone_type, last_seen, rank) "
            f"VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            rows,
        )
    return len(rows)


def sync_contact_enrichment(cur):
    data = _load("contact_enrichment")
    if not data:
        return {}
    details = (read_json(CONTACT_DETAILS_FILE, default={}) or {}).get("people") or []
    records = {(d.get("incident_id"), d.get("name")): d.get("chosen_record") for d in details}
    counts = {"people_enriched": 0, "next_of_kin": 0, "contact_points": 0}
    for entry in data.get("people") or []:
        name, incident_id = entry.get("name"), entry.get("incident_id")
        if not name or not incident_id:
            continue
        ensure_incident(cur, incident_id)
        deceased = entry.get("deceased") or {}
        first, last = split_name(name)
        person_id = upsert_person(cur, incident_id, person_key(name, 0), {
            "full_name": name, "first_name": first, "last_name": last, "age": to_int(entry.get("age")),
            "outcome": "killed", "aliases": _list(deceased.get("aliases")), "dob": to_date(deceased.get("dob")),
            "date_of_death": to_date(deceased.get("date_of_death")), "gender": deceased.get("gender"),
            "street": deceased.get("street"), "town": deceased.get("town"), "address_county": deceased.get("county"),
            "state": deceased.get("state"), "zip": deceased.get("zip"), "other_locations": _list(deceased.get("other_locations")),
            "match_status": entry.get("match"), "searchbug_token": deceased.get("searchbug_token"),
            "searchbug_record": db.jsonb(records.get((incident_id, name))) if records.get((incident_id, name)) else None,
            "relatives_on_record": to_int(entry.get("relatives_on_record")),
            "enriched_at": to_timestamp(data.get("run_at")),
        })
        counts["people_enriched"] += 1

        # Replace this person's contacts and next of kin with the latest lookup.
        cur.execute("DELETE FROM contact_points WHERE person_id = %s", (person_id,))
        cur.execute("DELETE FROM next_of_kin WHERE person_id = %s", (person_id,))
        counts["contact_points"] += insert_contacts(cur, "person_id", person_id, deceased.get("phones"), deceased.get("emails"))
        for rank, kin in enumerate(entry.get("relatives") or [], start=1):
            cur.execute(
                "INSERT INTO next_of_kin (person_id, rank, full_name, relationship, relationship_source, relationship_quote, "
                "address, searchbug_token, lookup_status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING kin_id",
                (person_id, rank, kin.get("name"), kin.get("relationship"), kin.get("relationship_source"),
                 kin.get("relationship_quote"), kin.get("address"), kin.get("token"), kin.get("lookup")),
            )
            kin_id = cur.fetchone()[0]
            counts["next_of_kin"] += 1
            counts["contact_points"] += insert_contacts(cur, "kin_id", kin_id, kin.get("phones"), kin.get("emails"))
    return counts


SYNCERS = {
    "source_collection": sync_source_collection,
    "link_extraction": sync_link_extraction,
    "incident_grouping": sync_incident_grouping,
    "severity_screening": sync_severity_screening,
    "lead_verification": sync_lead_verification,
    "contact_enrichment": sync_contact_enrichment,
}


def sync_stage(stage):
    """Load one stage's output into the tables (one transaction). Returns row counts."""
    syncer = SYNCERS.get(stage)
    if syncer is None:
        return {}
    with db.transaction() as cur:
        return syncer(cur)


def sync_all():
    """Load every stage's current output, in pipeline order."""
    return {stage: sync_stage(stage) for stage in SYNCERS}


def record_run(stage, started_at, finished_at, ok):
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO pipeline_runs (run_id, stage, started_at, finished_at, ok) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id, stage) DO UPDATE SET started_at = EXCLUDED.started_at, "
            "finished_at = EXCLUDED.finished_at, ok = EXCLUDED.ok",
            (run_id(), stage, started_at, finished_at, ok),
        )
