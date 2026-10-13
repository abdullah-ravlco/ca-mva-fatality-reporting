import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

try:
    from zoneinfo import ZoneInfo
    PUBLISHER_TZ = ZoneInfo("America/Los_Angeles")
except Exception:  # zoneinfo missing: fall back to UTC-7
    PUBLISHER_TZ = timezone(timedelta(hours=-7))


# Incidents within PREFERRED_DAYS are normal leads; up to MAX_DAYS they are kept
# but flagged as older; anything older is skipped. Unknown dates are kept.
PREFERRED_DAYS = 6
MAX_DAYS = 10

# Leads are people who died or were seriously injured, and did not cause the crash.
LEAD_OUTCOMES = {"killed": "fatality", "major_injury": "major_injury"}

# Fallback when outcome_category is missing (records analysed before it existed).
KILLED_RE = re.compile(r"\b(killed|died|dies|dead|deceased|fatal(?:ly)?|pronounced)\b", re.I)
MINOR_RE = re.compile(r"\b(non[- ]life[- ]threatening|non[- ]serious|minor|moderate|complaint of pain|treated and released)\b", re.I)
MAJOR_RE = re.compile(r"\b(serious(?:ly)?|severe(?:ly)?|critical(?:ly)?|major|grave|life[- ]threatening|great bodily)\b", re.I)
INJURED_RE = re.compile(r"\b(injur\w*|hurt|hospitali[sz]ed|wounded)\b", re.I)
UNINJURED_RE = re.compile(r"\b(uninjured|unharmed|not injured|unhurt|no injuries)\b", re.I)
CAUSED_RE = re.compile(r"\b(arrest\w*|cited|booked|fled|suspect\w*|charged|DUI|under the influence)\b", re.I)

# Explicitly out-of-state locations are skipped; missing/unclear locations are kept.
OTHER_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "Colorado", "Connecticut", "Delaware",
    "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
    "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
    "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia",
    "Wisconsin", "Wyoming",
]
OTHER_STATE_RE = re.compile(r"\b(" + "|".join(OTHER_STATES) + r")\b")
OTHER_STATE_ABBR_RE = re.compile(
    r",\s*(AL|AK|AZ|AR|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|"
    r"NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b(?:\s+\d{5})?\s*$"
)
CALIFORNIA_RE = re.compile(r"\b(California|Calif\.|CA)\b")


def parse_incident_date(value):
    """Parse an incident date only when the year is explicitly available."""
    if not value or not isinstance(value, str):
        return None

    value = value.strip()

    # Standard ISO date, e.g. 2026-10-10
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        pass

    # Other common formats, provided they include a year.
    formats = [
        "%B %d, %Y",
        "%b %d, %Y",
        "%b. %d, %Y",
        "%m/%d/%Y",
        "%Y/%m/%d",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    # Do not guess a year for dates such as "Oct. 6".
    return None


# Law-firm marketing sites repost crash news; they are weaker than news/official sources.
LAW_FIRM_HOST_RE = re.compile(r"law|legal|attorney|lawyer|injury", re.I)

NAME_TITLE_RE = re.compile(r"^(officer|deputy|sgt\.?|sergeant|dr\.?|mr\.?|mrs\.?|ms\.?)\s+", re.I)

# Conflicting dates for the same person more than this far apart are flagged.
DATE_CONFLICT_DAYS = 1


def date_status(incident_date, today):
    """(keep, status, note) for a known incident date."""
    age_days = (today - incident_date).days
    if age_days < 0:
        return True, "after_run_date", "Extracted date is after today (likely an extraction error); confirm it."
    if age_days <= PREFERRED_DAYS:
        return True, "recent", f"Incident {age_days} day(s) ago."
    if age_days <= MAX_DAYS:
        return True, "older", f"Incident {age_days} days ago (beyond {PREFERRED_DAYS}, within {MAX_DAYS})."
    return False, "too_old", f"Incident {age_days} days ago (more than {MAX_DAYS} days)."


def published_local_date(value):
    """Publication date in California time, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return parse_incident_date(value)
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(PUBLISHER_TZ).date()


def article_dates(article):
    """(incident_date, basis, published_date) for one analysed article.

    basis: "article" (the LLM read it from the text) or "pipeline" (source
    collection resolved it, e.g. "Friday" against the publication date).
    """
    published = published_local_date(article.get("published_at"))

    incident = parse_incident_date(article.get("incident_date"))
    basis = "article"
    if incident is None:
        incident = parse_incident_date(article.get("pipeline_incident_date"))
        basis = "pipeline"
    if incident is None:
        basis = None

    # An incident cannot happen after the article about it was published.
    if incident and published and incident > published + timedelta(days=1):
        incident, basis = None, None

    return incident, basis, published


def outside_california(location):
    """True only when the location explicitly names another state."""
    if not location or CALIFORNIA_RE.search(location):
        return False
    return bool(OTHER_STATE_RE.search(location) or OTHER_STATE_ABBR_RE.search(location))


def outcome_category(person):
    """killed | major_injury | minor_injury | injured_severity_unknown | uninjured | unknown."""
    category = person.get("outcome_category")
    if category and category != "unknown":
        return category
    text = " ".join(filter(None, [person.get("outcome"), person.get("outcome_evidence")]))
    if UNINJURED_RE.search(text):
        return "uninjured"
    if KILLED_RE.search(text):
        return "killed"
    if MINOR_RE.search(text):
        return "minor_injury"
    if MAJOR_RE.search(text):
        return "major_injury"
    if INJURED_RE.search(text):
        return "injured_severity_unknown"
    return "unknown"


def caused_crash_reason(person):
    """Why this person looks like the one who caused the crash, or None."""
    if person.get("caused_crash") is True:
        return "Article says this person caused the crash."
    if person.get("fault_status") == "reported_at_fault":
        return "Article reports this person at fault."
    if person.get("caused_crash") is False:
        return None
    if CAUSED_RE.search(person.get("outcome") or ""):
        return f"Person's outcome is '{person.get('outcome')}' (the driver involved, not a victim)."
    return None


def lead_reason(person):
    """(lead_type, None) for a dead/seriously injured victim, else (None, skip reason)."""
    caused = caused_crash_reason(person)
    if caused:
        return None, caused
    category = outcome_category(person)
    lead_type = LEAD_OUTCOMES.get(category)
    if not lead_type:
        return None, f"Not killed or seriously injured (outcome_category '{category}', outcome '{person.get('outcome')}')."
    return lead_type, None


def name_tokens(name):
    """Lower-case ASCII name tokens without titles/punctuation: 'José Ivan' -> ['jose', 'ivan']."""
    name = NAME_TITLE_RE.sub("", name or "")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return [token for token in re.split(r"[^a-z]+", name.lower()) if token]


def possible_fault(person, name):
    """Note when the article suggests the victim contributed to the crash."""
    if person.get("fault_status") == "alleged_at_fault":
        return "Article suggests this person may have been at fault: " + (person.get("fault_evidence") or "see source")
    evidence = (person.get("fault_evidence") or "").lower()
    tokens = [t for t in name_tokens(name) if len(t) > 2]
    if evidence and any(t in name_tokens(evidence) for t in tokens):
        return "Fault evidence mentions this person: " + person["fault_evidence"]
    return None


def is_law_firm_source(url):
    return bool(LAW_FIRM_HOST_RE.search(urlsplit(url or "").netloc))


def _skip(article, name, reason):
    return {
        "article_title": article.get("article_title"),
        "source_url": article.get("source_url"),
        "case_id": article.get("case_id"),
        "person_name": name,
        "skip_reason": reason,
    }


# --------------------------------------------------
# Candidates (one per named person per article)
# --------------------------------------------------

def candidates_from_articles(articles):
    """(candidates, skipped). Date rules are applied after duplicates are merged,
    because another article about the same person may give the date."""
    candidates, skipped = [], []

    for article in articles:
        named = [
            person for person in article.get("people") or []
            if isinstance(person.get("person_name"), str) and person["person_name"].strip()
        ]
        if not named:
            continue

        incident, basis, published = article_dates(article)
        location = article.get("accident_location")

        for person in named:
            name = " ".join(person["person_name"].split())

            if outside_california(location):
                skipped.append(_skip(article, name, f"Location is outside California: {location}"))
                continue
            lead_type, reason = lead_reason(person)
            if reason:
                skipped.append(_skip(article, name, reason))
                continue

            age = person.get("person_age")
            if isinstance(age, bool) or not isinstance(age, int):
                age = None

            candidates.append({
                "article": article,
                "person": person,
                "name": name,
                "tokens": name_tokens(name),
                "age": age,
                "lead_type": lead_type,
                "incident_date": incident,
                "date_basis": basis,
                "published_date": published,
                "law_firm": is_law_firm_source(article.get("source_url")),
            })

    return candidates, skipped


# --------------------------------------------------
# Duplicate people across articles
# --------------------------------------------------

def same_person(a, b):
    """Same name (first + last token, or one name contained in the other) and no age conflict."""
    if a["age"] is not None and b["age"] is not None and a["age"] != b["age"]:
        return False
    ta, tb = a["tokens"], b["tokens"]
    if not ta or not tb:
        return False
    if len(ta) == 1 or len(tb) == 1:
        # First name only: merge only with the same crash.
        return ta == tb and a["article"].get("case_id") == b["article"].get("case_id")
    if ta[0] == tb[0] and ta[-1] == tb[-1]:
        return True
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return set(shorter) <= set(longer) and len(shorter) >= 2


def group_people(candidates):
    groups = []
    for candidate in candidates:
        for group in groups:
            if any(same_person(candidate, member) for member in group):
                group.append(candidate)
                break
        else:
            groups.append([candidate])
    return groups


def candidate_rank(candidate):
    """Higher is better: date from the article, news/official source, age, longer name."""
    confidence = {"high": 2, "medium": 1}.get(candidate["article"].get("extraction_confidence"), 0)
    return (
        candidate["date_basis"] == "article",
        candidate["incident_date"] is not None,
        not candidate["law_firm"],
        candidate["age"] is not None,
        confidence,
        len(candidate["tokens"]),
    )


def merged_dates(group):
    """(incident_date, basis, conflict_note) from all articles about one person."""
    dated = [c for c in group if c["incident_date"]]
    if not dated:
        return None, None, None
    best = max(dated, key=lambda c: (c["date_basis"] == "article", not c["law_firm"]))
    others = {c["incident_date"] for c in dated}
    spread = (max(others) - min(others)).days
    note = None
    if spread > DATE_CONFLICT_DAYS:
        note = "Sources give different incident dates: " + ", ".join(sorted(d.isoformat() for d in others))
    return best["incident_date"], best["date_basis"], note


# --------------------------------------------------
# Shortlist
# --------------------------------------------------

def build_entry(group, today):
    """(entry, skip_reason) for one merged person."""
    best = max(group, key=candidate_rank)
    article, person = best["article"], best["person"]

    incident, basis, conflict_note = merged_dates(group)
    published_dates = [c["published_date"] for c in group if c["published_date"]]
    first_published = min(published_dates) if published_dates else None

    review_notes = []
    if incident:
        keep, status, note = date_status(incident, today)
        if not keep:
            return None, note
        if basis == "pipeline":
            note += " (date resolved by source collection, e.g. from a weekday; confirm it)"
        review_notes.append(note)
    elif first_published:
        published_days = (today - first_published).days
        if published_days > MAX_DAYS:
            return None, (
                f"Incident date unknown and the first article was published {published_days} days ago "
                f"(more than {MAX_DAYS}), so the crash is at least that old."
            )
        status = "unknown"
        review_notes.append(
            f"Incident date not stated; first article published {first_published.isoformat()} "
            f"({published_days} day(s) ago), so the crash was on or before that date."
        )
    else:
        status = "unknown"
        review_notes.append("Incident date and publication date unknown; confirm from the source.")
    if conflict_note:
        review_notes.append(conflict_note)

    age = best["age"] if best["age"] is not None else next((c["age"] for c in group if c["age"] is not None), None)
    location = article.get("accident_location") or next(
        (c["article"].get("accident_location") for c in group if c["article"].get("accident_location")), None
    )

    flags = []
    if len(best["tokens"]) < 2:
        flags.append("first_name_only")
        review_notes.append("Only a first name is reported; full name still needed.")
    fault_note = next((possible_fault(c["person"], c["name"]) for c in group if possible_fault(c["person"], c["name"])), None)
    if fault_note:
        flags.append("possible_fault")
        review_notes.append(fault_note)
    if all(c["law_firm"] for c in group):
        flags.append("law_firm_source_only")
        review_notes.append("Only found on law-firm sites; confirm with a news or official source.")
    if age is None:
        review_notes.append("Age not stated in the article.")
    if not location:
        flags.append("location_unknown")
        review_notes.append("Accident location not found; confirm it is in California.")
    if status == "unknown":
        flags.append("date_unknown")

    sources = []
    for candidate in sorted(group, key=candidate_rank, reverse=True):
        url = candidate["article"].get("source_url")
        if url and url not in [s["source_url"] for s in sources]:
            sources.append({
                "source_url": url,
                "article_title": candidate["article"].get("article_title"),
                "case_id": candidate["article"].get("case_id"),
                "published_date": candidate["published_date"].isoformat() if candidate["published_date"] else None,
                "law_firm_site": candidate["law_firm"],
            })

    entry = {
        "lead_status": "needs_review" if flags else "clear",
        "lead_flags": flags,
        "lead_type": best["lead_type"],
        "person_name": max((c["name"] for c in group), key=lambda n: len(name_tokens(n))),
        "person_age": age,
        "person_role": person.get("person_role"),
        "outcome": person.get("outcome"),
        "outcome_category": outcome_category(person),
        "outcome_evidence": person.get("outcome_evidence"),
        "caused_crash": person.get("caused_crash"),
        "fault_status": person.get("fault_status"),
        "fault_evidence": person.get("fault_evidence"),
        "incident_date": incident.isoformat() if incident else None,
        "incident_date_status": status,
        "incident_date_basis": basis,
        "incident_time": article.get("incident_time"),
        "accident_location": location,
        "article_title": article.get("article_title"),
        "source_url": article.get("source_url"),
        "case_id": article.get("case_id"),
        "case_ids": sorted({c["article"].get("case_id") for c in group if c["article"].get("case_id")}),
        "source_count": len(sources),
        "sources": sources,
        "extraction_confidence": article.get("extraction_confidence"),
        "review_required": True,
        "review_notes": review_notes,
    }
    return entry, None


def build_shortlist_report(articles, today=None):
    """(shortlisted, skipped) for every named person in the analysed articles.

    Rules:
      - the person died or was seriously injured, is named (age optional),
        and is not the person who caused the crash;
      - the same person reported by several articles appears once, with all
        sources listed;
      - incident date within MAX_DAYS; an unknown date is kept unless the
        first article about it was published more than MAX_DAYS ago;
      - location not explicitly outside California (unknown location is kept).
    Entries are "clear" or "needs_review" (lead_flags say why).
    """
    today = today or date.today()
    candidates, skipped = candidates_from_articles(articles)
    shortlisted = []

    for group in group_people(candidates):
        entry, reason = build_entry(group, today)
        if entry:
            shortlisted.append(entry)
            continue
        for candidate in group:
            skipped.append(_skip(candidate["article"], candidate["name"], reason))

    # Clear leads first, then most recent; unknown dates last.
    shortlisted.sort(key=lambda e: e["incident_date"] or "", reverse=True)
    shortlisted.sort(key=lambda e: (e["lead_status"] != "clear", e["incident_date"] is None))
    return shortlisted, skipped


def build_shortlist(articles):
    """Shortlisted people only (see build_shortlist_report)."""
    return build_shortlist_report(articles)[0]
