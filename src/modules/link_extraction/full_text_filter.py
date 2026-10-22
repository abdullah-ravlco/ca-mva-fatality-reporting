"""Re-check each fetched article on its full text, before any LLM call.

Source collection could often only read a headline (Google News links can't be
opened there), so its crash-date and location checks were mostly "unknown". Here
the same rules run on the article text:

  drop - the text shows the crash is before the collection window, outside
         California, happened earlier (follow-up story), or is not a fatal road crash
  keep - everything else, including articles whose date/location stay unknown

Articles that could not be fetched are judged on their RSS title/description only,
so they are kept unless that metadata already rules them out.
"""

from src.core.date_utils import parse_date
from src.modules.source_collection.collectors.rss import LOCATION_TEXT_CHARS
from src.modules.source_collection.filters import looks_relevant
from src.modules.source_collection.incident_dates import NOTE_EARLIER, find_incident_date
from src.modules.source_collection.location import CALIFORNIA_CONFIRMED, OUTSIDE_CALIFORNIA, detect_location
from src.modules.source_collection.records import INCIDENT_OUTSIDE_WINDOW, news_status

KEEP = "keep"
DROP = "drop"
OFFICIAL_SOURCE_TYPES = ("coroner", "official_listing")
RELEVANCE_TEXT_CHARS = 1500


def publication_date(article):
    """Page metadata date first, then the feed's publication date."""
    metadata = article.get("metadata") or {}
    return parse_date(metadata.get("date_published")) or parse_date(article.get("published_at"))


def _official_facts(article):
    """Coroner/CHP records: California by definition; the record's own crash date counts."""
    return {
        "location_status": CALIFORNIA_CONFIRMED,
        "location_evidence": article.get("location_evidence") or article.get("publisher"),
    }


def apply_full_text_filter(article, now, since):
    """Add crash-date, location, status and filter_decision fields to one article record."""
    title = article.get("title") or ""
    text = article.get("text") or ""
    description = article.get("description") or ""
    body = text or description
    published = publication_date(article)

    incident = find_incident_date(" ".join(p for p in (title, body) if p), published)
    location = detect_location(title, body[:LOCATION_TEXT_CHARS])

    official = article.get("source_type") in OFFICIAL_SOURCE_TYPES
    if official:
        location.update(_official_facts(article))
        if incident["incident_date"] is None and article.get("case_incident_date"):
            incident.update(
                incident_date=parse_date(article["case_incident_date"]),
                incident_date_basis="official_record",
                incident_date_note=None,
                incident_date_note_type=None,
            )

    status, reasons = news_status(incident["incident_date"], location["location_status"], now, since)
    if incident["incident_date_note"]:
        reasons.append(incident["incident_date_note"])

    drop_reasons = []
    if status == INCIDENT_OUTSIDE_WINDOW:
        drop_reasons.append("crash date in the article is before the collection window")
    if incident["incident_date_note_type"] == NOTE_EARLIER:
        drop_reasons.append("article says the crash happened earlier")
    if location["location_status"] == OUTSIDE_CALIFORNIA:
        drop_reasons.append(f"article places the crash outside California ({location['location_evidence']})")
    # Relevance is only re-judged on real article text; the headline already passed it.
    if text and not official and not looks_relevant(title, text[:RELEVANCE_TEXT_CHARS]):
        drop_reasons.append("article text does not describe a fatal road crash")

    article.update(location)
    article.update({
        "published_at_resolved": published.isoformat() if published else None,
        "incident_date": incident["incident_date"].isoformat() if incident["incident_date"] else None,
        "incident_date_text": incident["incident_date_text"],
        "incident_date_basis": incident["incident_date_basis"],
        "incident_date_evidence": incident["incident_date_evidence"],
        "incident_date_note": incident["incident_date_note"],
        "incident_date_note_type": incident["incident_date_note_type"],
        "status": status,
        "filter_decision": DROP if drop_reasons else KEEP,
        "filter_reasons": drop_reasons or reasons,
        "filter_basis": "article_text" if text else "rss_metadata",
    })
    return article
