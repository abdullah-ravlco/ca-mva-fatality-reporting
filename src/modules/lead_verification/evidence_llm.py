"""Ask the LLM to read all sources for one incident and list every fact with
the source it came from and an exact quote. The LLM never decides whether a
fact is verified; verifier.py does that by checking the quotes."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.modules.lead_verification.config import MAX_SOURCE_CHARS, MAX_SOURCES_PER_LLM_CALL

# Reuse step 3's OpenAI client and JSON request helper (retries + fallback model).
from src.modules.article_analysis.llm_extractor import build_llm, llm_model, request_json


class Claim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: Optional[str] = None
    source: Optional[str] = None
    quote: Optional[str] = None

    @field_validator("value", "source", "quote", mode="before")
    @classmethod
    def as_text(cls, value):
        if value is None or isinstance(value, bool):
            return None
        return str(value).strip() or None


def as_claim_list(value):
    """The model sometimes returns one claim {...} or null instead of [{...}]."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


class PersonEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator("full_name", "age", "residence", "outcome", "role", "driver_of_vehicle", "fault", mode="before")
    @classmethod
    def claim_lists(cls, value):
        return as_claim_list(value)

    shortlist_name: Optional[str] = None
    full_name: List[Claim] = Field(default_factory=list)
    age: List[Claim] = Field(default_factory=list)
    residence: List[Claim] = Field(default_factory=list)
    outcome: List[Claim] = Field(default_factory=list)
    role: List[Claim] = Field(default_factory=list)
    driver_of_vehicle: List[Claim] = Field(default_factory=list)
    fault: List[Claim] = Field(default_factory=list)


class IncidentEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    incident_date: List[Claim] = Field(default_factory=list)
    incident_time: List[Claim] = Field(default_factory=list)
    location: List[Claim] = Field(default_factory=list)
    crash_type: List[Claim] = Field(default_factory=list)
    other_party: List[Claim] = Field(default_factory=list)
    description: Optional[str] = None
    description_sources: List[str] = Field(default_factory=list)
    people: List[PersonEvidence] = Field(default_factory=list)

    @field_validator("incident_date", "incident_time", "location", "crash_type", "other_party", mode="before")
    @classmethod
    def claim_lists(cls, value):
        return as_claim_list(value)

    @field_validator("people", mode="before")
    @classmethod
    def people_list(cls, value):
        if isinstance(value, dict):
            return [value]
        return [item for item in value or [] if isinstance(item, dict)]

    @field_validator("description", mode="before")
    @classmethod
    def description_text(cls, value):
        return value if isinstance(value, str) and value.strip() else None

    @field_validator("description_sources", mode="before")
    @classmethod
    def as_list(cls, value):
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value or [] if v]


def rank_sources(sources):
    """Official first, then news, law-firm sites last; already-fetched before new."""
    def key(source):
        return (not source["official"], source["law_firm"], source["origin"] != "already_fetched")
    return sorted(sources, key=key)[:MAX_SOURCES_PER_LLM_CALL]


def _sources_block(sources):
    blocks = []
    for source in sources:
        blocks.append(
            f"[{source['id']}] {source.get('title') or ''}\n"
            f"URL: {source['url']}\n"
            f"Published: {source.get('published_at') or 'unknown'}\n"
            f"Official source: {'yes' if source['official'] else 'no'}\n"
            f"TEXT:\n{source['text'][:MAX_SOURCE_CHARS]}"
        )
    return "\n\n-----\n\n".join(blocks)


def build_prompt(incident, sources):
    names = "\n".join(f"- {name}" for name in incident["names"])
    return f"""
You verify facts about one California traffic crash for a records review.
Use ONLY the numbered sources below. Never use outside knowledge.

SHORTLISTED PEOPLE (killed or seriously injured in this crash):
{names}

SOURCES:
{_sources_block(sources)}

Return one JSON object:
{{
  "incident_date": [claim],
  "incident_time": [claim],
  "location": [claim],
  "crash_type": [claim],
  "other_party": [claim],
  "description": string or null,
  "description_sources": ["S1", ...],
  "people": [
    {{
      "shortlist_name": exactly one name from SHORTLISTED PEOPLE,
      "full_name": [claim],
      "age": [claim],
      "residence": [claim],
      "outcome": [claim],
      "role": [claim],
      "driver_of_vehicle": [claim],
      "fault": [claim]
    }}
  ]
}}

A claim is {{"value": string, "source": "S#", "quote": string}}.
Every [claim] field is a JSON LIST of claims, even when there is only one:
"age": [{{"value": "35", "source": "S1", "quote": "..."}}]. Never a single object.

RULES:
1. Give one claim PER SOURCE that states the fact (several sources can
   support the same fact). If no source states it, use an empty list [].
2. "quote" must be copied character-for-character from that source's TEXT,
   short (under 40 words), and must contain the fact. Never paraphrase.
3. incident_date value: YYYY-MM-DD. If a source says "Friday" or "Oct. 8",
   resolve it with that source's Published date (the most recent such day
   on or before publication). If it cannot be resolved, leave it out.
   A date of an arrest, identification, funeral or fundraiser is not the
   incident date.
4. incident_time value: as written (e.g. "4:15 p.m.").
5. location: where the crash happened (road, cross street, city), as
   written. Not where someone lived.
6. full_name: the name exactly as the source spells it.
7. age: a number only, as stated.
8. residence: where the person lived, exactly as the source states it
   (usually a city, e.g. "of Lamont" -> "Lamont"). Only what the source
   says; never guess.
9. outcome: "killed", "major_injury" (serious, critical, life-threatening
   injuries) or "minor_injury" ("non-life-threatening", minor, moderate).
10. role: driver, passenger, pedestrian, cyclist, motorcyclist or other.
11. fault, about THIS person only:
    - "at_fault": a source says this person caused the crash (e.g. ran a
      red light, was speeding, DUI, crossed into oncoming traffic, lost
      control, was arrested or cited for the crash).
    - "not_at_fault": a source says another driver/vehicle caused it or
      struck this person, or the other driver was arrested/cited/DUI.
    - A passenger was not driving: do not mark a passenger "at_fault"
      for how the vehicle was driven.
    - "not_established": a source says the cause is under investigation
      or unknown.
    Quote the sentence that shows it.
12. crash_type: "single_vehicle" (one vehicle only: went off the road,
    hit a tree/pole/wall, overturned), "multi_vehicle" (two or more
    vehicles collided), or "pedestrian_or_cyclist_struck".
13. other_party: ONLY a vehicle/driver OUTSIDE the shortlisted person's
    own vehicle (e.g. "the 16-year-old driver of the SUV was arrested").
    The driver or passengers of the person's own vehicle are NOT the other
    party. For a single_vehicle crash, other_party must be [].
14. driver_of_vehicle (passengers only): who was driving the vehicle this
    person rode in and what happened to them, as written (e.g. "her
    husband, a 33-year-old Hesperia man, who also died"). [] for drivers,
    pedestrians and cyclists.
15. description: 2-3 neutral sentences on what happened, using only facts
    from the sources. description_sources lists the sources used.
16. Include every SHORTLISTED person, even if nothing is found (empty lists).
17. Return JSON only.
"""


def extract_evidence(client, incident, sources):
    """IncidentEvidence for the ranked sources (raises on LLM failure)."""
    prompt = build_prompt(incident, sources)
    title = incident["people"][0].get("article_title") or incident["incident_key"]
    data = request_json(client, llm_model(), prompt, title)
    if not isinstance(data, dict):
        raise ValueError("The model response must be a JSON object.")
    return IncidentEvidence.model_validate(data)


__all__ = ["build_llm", "llm_model", "extract_evidence", "rank_sources"]
