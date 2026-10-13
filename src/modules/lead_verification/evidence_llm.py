"""Ask the LLM to read all sources for one incident and list every fact with
the source it came from and an exact quote. The LLM never decides whether a
fact is verified; verifier.py does that by checking the quotes.

One call covers one incident (all of its articles), and the LLM lists every
person involved - named or not - so several victims of one crash stay separate
and one victim reported by several articles stays one person.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from src.core.llm_client import build_llm, llm_model, request_structured
from src.modules.lead_verification.config import MAX_SOURCE_CHARS, MAX_SOURCES_PER_LLM_CALL


STATEMENT_TYPES = (
    "official_finding",            # an official determination (coroner, final CHP/police report, court)
    "attributed_to_authorities",   # police/CHP/investigators said it, preliminary
    "allegation_or_preliminary",   # witnesses, family, unattributed or "believed to"
    "citation_or_arrest",          # someone was arrested, cited or charged
    "under_investigation",         # cause/fault explicitly under investigation or unknown
)
INDICATES = ("responsibility", "no_responsibility", "undetermined")


class Claim(BaseModel):
    value: Optional[str] = None
    source: Optional[str] = None
    quote: Optional[str] = None

    @field_validator("value", "source", "quote")
    @classmethod
    def as_text(cls, value):
        return (value or "").strip() or None


class FaultStatement(Claim):
    """What a source says about this person's responsibility, and who says it."""

    statement_type: Optional[Literal[STATEMENT_TYPES]] = None
    attributed_to: Optional[str] = None
    indicates: Literal[INDICATES]

    @field_validator("attributed_to")
    @classmethod
    def attribution_text(cls, value):
        return (value or "").strip() or None


# These models are also the response schema sent to the model (structured
# outputs), where every field is required; the defaults only apply to objects
# built in code.
class PersonEvidence(BaseModel):
    person_ref: Optional[str] = None
    descriptor: Optional[str] = None
    full_name: List[Claim] = Field(default_factory=list)
    age: List[Claim] = Field(default_factory=list)
    residence: List[Claim] = Field(default_factory=list)
    outcome: List[Claim] = Field(default_factory=list)
    role: List[Claim] = Field(default_factory=list)
    driver_of_vehicle: List[Claim] = Field(default_factory=list)
    fault_statements: List[FaultStatement] = Field(default_factory=list)


class IncidentEvidence(BaseModel):
    incident_date: List[Claim] = Field(default_factory=list)
    incident_time: List[Claim] = Field(default_factory=list)
    location: List[Claim] = Field(default_factory=list)
    crash_type: List[Claim] = Field(default_factory=list)
    other_party: List[Claim] = Field(default_factory=list)
    description: Optional[str] = None
    description_sources: List[str] = Field(default_factory=list)
    people: List[PersonEvidence] = Field(default_factory=list)

    @field_validator("description")
    @classmethod
    def description_text(cls, value):
        return (value or "").strip() or None


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
    return f"""
You extract facts about one reported California traffic crash for a records review.
The sources below were grouped as reports of the same crash; they may still
describe different people involved in it. Use ONLY the numbered sources.
Never use outside knowledge, never guess, never fill gaps.

SOURCES:
{_sources_block(sources)}

Fill the response fields:
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
      "person_ref": "P1",
      "descriptor": string,
      "full_name": [claim],
      "age": [claim],
      "residence": [claim],
      "outcome": [claim],
      "role": [claim],
      "driver_of_vehicle": [claim],
      "fault_statements": [fault_statement]
    }}
  ]
}}

A claim is {{"value": string, "source": "S#", "quote": string}}.
A fault_statement is a claim plus
  "statement_type": one of {", ".join(STATEMENT_TYPES)},
  "attributed_to": who made the statement, as written (e.g. "CHP", "police",
                   "a witness"), or null if unattributed,
  "indicates": "responsibility" | "no_responsibility" | "undetermined".

RULES:
1. Give one claim PER SOURCE that states the fact. If no source states it, [].
2. "quote" must be copied character-for-character from that source's TEXT,
   short (under 40 words), and must contain the fact. Never paraphrase.
3. incident_date value: YYYY-MM-DD. Resolve "Friday" or "Oct. 8" with that
   source's Published date (the most recent such day on or before publication).
   If it cannot be resolved, leave it out. Dates of an arrest, identification,
   funeral or fundraiser are not the incident date.
4. incident_time: as written. location: where the crash happened (road, cross
   street, city), as written; not where anyone lived.
5. PEOPLE: list every person the sources describe as involved in this crash
   (killed, injured, uninjured drivers, passengers, pedestrians, cyclists).
   - One entry per real person. Use person_ref P1, P2, ... in order.
   - Treat mentions in different sources as the same person only when the
     name matches, or (no name) the age, role and outcome all match.
   - Never merge two different names into one person. Two people killed in
     the same crash are two entries.
   - Include unnamed people. descriptor is always a short neutral description
     (e.g. "unnamed 34-year-old motorcyclist"); full_name stays [] when no
     source names them. Never invent or complete a name.
6. full_name: exactly as spelled. age: a number only, as stated.
7. residence: only what a source states (usually a city, e.g. "of Lamont" ->
   "Lamont"). Never a street address. Never guess.
8. outcome: "killed", "major_injury" (serious, critical, life-threatening),
   "minor_injury" (non-life-threatening, minor, moderate), "injured_unknown"
   (injured, severity not stated) or "uninjured".
9. role: driver, passenger, pedestrian, cyclist, motorcyclist or other.
10. fault_statements, about THIS person only. Record only what a source says;
    do not decide fault yourself.
    - "responsibility": a source says or alleges THIS person caused or
      contributed to the crash (e.g. police said he ran a red light).
    - "no_responsibility": ONLY when a source explicitly says THIS person was
      not at fault / not responsible / did nothing wrong.
    - "undetermined": the cause is under investigation, unknown, or the
      statement does not say who was responsible.
    - Being struck by another vehicle is NOT "no_responsibility".
    - Being a passenger is NOT "no_responsibility".
    - Another person being arrested, cited or charged is NOT
      "no_responsibility" for this person; record it on that other person
      as statement_type "citation_or_arrest".
    - An arrest, citation or charge is "citation_or_arrest", never
      "official_finding". A preliminary police statement is
      "attributed_to_authorities", never "official_finding".
    - "official_finding" only for a stated official determination (e.g. a
      coroner ruling, a completed investigation's finding, a conviction).
11. crash_type: "single_vehicle", "multi_vehicle" or "pedestrian_or_cyclist_struck".
12. other_party: ONLY a vehicle/driver outside the killed or injured person's own
    vehicle, as written. [] for a single-vehicle crash.
13. driver_of_vehicle (passengers only): who was driving the vehicle this person
    rode in, as written. [] otherwise.
14. description: 2-3 neutral sentences using only facts from the sources; no
    blame unless attributed (e.g. "CHP said ..."). description_sources lists them.
"""


def extract_evidence(client, incident, sources):
    """IncidentEvidence for the ranked sources (raises on LLM failure)."""
    prompt = build_prompt(incident, sources)
    return request_structured(client, llm_model(), prompt, IncidentEvidence, incident["incident_key"])


__all__ = ["build_llm", "llm_model", "extract_evidence", "rank_sources", "STATEMENT_TYPES"]
