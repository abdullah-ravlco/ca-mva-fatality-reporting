"""Check JEV's JSON reply and turn it into typed objects.

Small differences in form are normalized ("Killed" -> "killed", "34" -> 34, "unknown" as a name ->
null). A value outside the allowed set is an error: it is reported, never silently replaced.
"""

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# needs_review is no longer asked for; it is still accepted so an old-style reply is not rejected.
STATUSES = ("qualified", "victim_identity_unidentified", "fault_unidentified", "dropped", "needs_review")
NOT_STATED = {"", "unknown", "unidentified", "n/a", "na", "none", "null", "not reported", "not stated",
              "not specified", "not available", "unnamed"}


def _text(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value.lower() in NOT_STATED else value


def _enum(value):
    """'Multi-Vehicle ' -> 'multi_vehicle'; None -> None (the field default applies)."""
    if value is None:
        return None
    return re.sub(r"[\s-]+", "_", str(value).strip().lower())


class Victim(BaseModel):
    name: Optional[str] = None
    age: Optional[int] = None
    role: Literal["driver", "passenger", "pedestrian", "cyclist", "motorcyclist", "unknown"] = "unknown"
    outcome: Literal["killed", "injured", "unknown"] = "unknown"
    injury_tier: Literal["catastrophic", "serious", "moderate", "minor", "unknown"] = "unknown"
    catastrophic_items: List[str] = Field(default_factory=list)
    injury_quote: Optional[str] = None
    injury_source_url: Optional[str] = None
    rode_with: Optional[str] = None
    at_fault_party: Literal["other_party", "this_victim", "own_driver", "unknown"] = "unknown"
    fault_basis: Literal[
        "official_finding", "police_statement", "arrest_or_citation", "described_in_article", "not_reported"
    ] = "not_reported"
    fault_quote: Optional[str] = None
    fault_source_url: Optional[str] = None
    impaired: Literal["yes", "no", "unknown"] = "unknown"
    impaired_quote: Optional[str] = None
    impaired_source_url: Optional[str] = None

    @field_validator("name", "injury_quote", "injury_source_url", "rode_with", "fault_quote",
                     "fault_source_url", "impaired_quote", "impaired_source_url", mode="before")
    @classmethod
    def text(cls, value):
        return _text(value)

    @field_validator("age", mode="before")
    @classmethod
    def number(cls, value):
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        match = re.search(r"\d{1,3}", str(value))
        return int(match.group(0)) if match else None

    @field_validator("role", "outcome", "injury_tier", "at_fault_party", "fault_basis", "impaired", mode="before")
    @classmethod
    def enum(cls, value):
        return _enum(value) or "unknown"

    @field_validator("fault_basis", mode="before")
    @classmethod
    def basis(cls, value):
        value = _enum(value)
        return "not_reported" if value in (None, "unknown", "none") else value

    @field_validator("catastrophic_items", mode="before")
    @classmethod
    def items(cls, value):
        return [str(v).strip() for v in value or [] if str(v).strip()]


class OtherParty(BaseModel):
    involved: Literal["yes", "no", "unclear"] = "unclear"
    description: Optional[str] = None
    quote: Optional[str] = None
    source_url: Optional[str] = None

    @field_validator("involved", mode="before")
    @classmethod
    def enum(cls, value):
        value = _enum(value)
        return "unclear" if value in (None, "unknown") else value

    @field_validator("description", "quote", "source_url", mode="before")
    @classmethod
    def text(cls, value):
        return _text(value)


class JevResult(BaseModel):
    crash_date: Optional[str] = None
    crash_location: Optional[str] = None
    collision_type: Literal["multi_vehicle", "single_vehicle", "vehicle_struck_pedestrian_or_cyclist", "unknown"] = "unknown"
    victims: List[Victim] = Field(default_factory=list)
    other_party: OtherParty = Field(default_factory=OtherParty)
    conflicts: List[str] = Field(default_factory=list)
    case_status: Literal[STATUSES] = "dropped"
    drop_reasons: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    decision_reason: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("crash_date", "crash_location", "decision_reason", "notes", mode="before")
    @classmethod
    def text(cls, value):
        return _text(value)

    @field_validator("collision_type", mode="before")
    @classmethod
    def collision(cls, value):
        return _enum(value) or "unknown"

    @field_validator("case_status", mode="before")
    @classmethod
    def status(cls, value):
        return _enum(value) or "dropped"

    @field_validator("other_party", mode="before")
    @classmethod
    def party(cls, value):
        return value or {}

    @field_validator("victims", "conflicts", "tags", "drop_reasons", mode="before")
    @classmethod
    def lists(cls, value):
        return value or []
