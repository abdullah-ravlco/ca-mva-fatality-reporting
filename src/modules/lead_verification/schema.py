"""The structured result the lead-verification agent must return for one incident."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

YesNo = Literal["yes", "no", "unknown"]
Verdict = Literal["qualifies", "does_not_qualify", "needs_review"]

REASONS = (
    "no_death",                       # nobody died, or no source says anyone died
    "not_a_road_crash",               # medical event, suicide, intentional act, non-road incident
    "single_vehicle_no_other_party",  # no other driver/vehicle could be responsible
    "deceased_impaired",              # every deceased person was reported impaired (DUI, drunk, drugs)
    "deceased_at_fault",              # every deceased person was reported at fault
    "outside_california",
    "outside_window",                 # crash older than the lookback window
    "insufficient_information",
)
FAULT_BASES = (
    "stated_by_authorities",          # CHP/police/coroner said who caused it
    "other_party_cited_or_arrested",  # someone else was cited, arrested or charged
    "deceased_cited_or_arrested",
    "circumstances_described",        # fault follows from the facts reported (e.g. driver ran a red light)
    "none",                           # under investigation / not reported
)


NOT_STATED = {"unknown", "n/a", "na", "none", "null", "not stated", "not specified", "not available", "unnamed"}


def _text(value):
    """Stripped text, or None for empty values and "unknown"-style placeholders."""
    value = (value or "").strip()
    return None if value.lower() in NOT_STATED else (value or None)


class Fact(BaseModel):
    """A value with the page it came from and the sentence that states it."""

    value: Optional[str] = None
    source_url: Optional[str] = None
    quote: Optional[str] = None

    @field_validator("value", "source_url", "quote")
    @classmethod
    def as_text(cls, value):
        return _text(value)


class Person(BaseModel):
    name: Fact = Field(default_factory=Fact)
    age: Fact = Field(default_factory=Fact)
    # Where the person lived, only as a source states it (usually a city). Never searched for.
    address: Fact = Field(default_factory=Fact)
    outcome: Literal["killed", "injured", "uninjured", "unknown"] = "unknown"
    role: Literal["driver", "passenger", "pedestrian", "cyclist", "motorcyclist", "other", "unknown"] = "unknown"
    vehicle: Optional[str] = None
    at_fault: YesNo = "unknown"
    fault_basis: Literal[FAULT_BASES] = "none"
    fault_explanation: Optional[str] = None
    fault_source_url: Optional[str] = None
    fault_quote: Optional[str] = None
    impaired: YesNo = "unknown"          # alcohol / drugs, as reported
    impaired_source_url: Optional[str] = None
    impaired_quote: Optional[str] = None

    @field_validator("vehicle", "fault_explanation", "fault_source_url", "fault_quote", "impaired_source_url", "impaired_quote")
    @classmethod
    def as_text(cls, value):
        return _text(value)


class IncidentFacts(BaseModel):
    date: Fact = Field(default_factory=Fact)      # YYYY-MM-DD
    time: Fact = Field(default_factory=Fact)      # as written
    location: Fact = Field(default_factory=Fact)  # where the crash happened, as written
    city: Optional[str] = None
    county: Optional[str] = None
    road: Optional[str] = None
    crash_type: Literal["single_vehicle", "multi_vehicle", "pedestrian_or_cyclist_struck", "unknown"] = "unknown"
    other_party_involved: YesNo = "unknown"
    other_party_description: Optional[str] = None
    summary: Optional[str] = None

    @field_validator("city", "county", "road", "other_party_description", "summary")
    @classmethod
    def as_text(cls, value):
        return _text(value)


class AgentAssessment(BaseModel):
    verdict: Verdict
    reasons: List[Literal[REASONS]] = Field(default_factory=list)
    explanation: Optional[str] = None


class LeadResult(BaseModel):
    incident: IncidentFacts = Field(default_factory=IncidentFacts)
    people: List[Person] = Field(default_factory=list)
    assessment: AgentAssessment
    missing_information: List[str] = Field(default_factory=list)
    pages_used: List[str] = Field(default_factory=list)
