"""Decide whether an incident qualifies, from the facts the agent returned.

The agent gives its own verdict, but the rules here have the last word so the decision is
the same every time and can be explained:

  does not qualify when
    - nobody is reported killed                          (no_death)
    - no other party could be responsible: a single-vehicle crash with no other driver
      or vehicle, including a passenger killed in a crash their own driver caused
                                                         (single_vehicle_no_other_party)
    - every deceased person was reported impaired        (deceased_impaired)
    - every deceased person was reported at fault        (deceased_at_fault)
    - outside California                                 (outside_california)
    - the agent says it is not a road crash              (not_a_road_crash)
  qualifies when none of that applies, at least one deceased person is a possible claimant, and the
    sources show the other party caused the crash
  fault_unidentified when everything qualifies except that the other party's fault is not
    established (under investigation, a driver's medical emergency, not reported)
  needs_review when other facts needed are missing (no crash date/location, no known death...).

Fault is whatever the sources report. It is not a legal determination.
"""

from src.core.date_utils import parse_date
from src.modules.source_collection.location import OUTSIDE_CALIFORNIA, detect_location


def _deceased(result):
    return [p for p in result.people if p.outcome == "killed"]


def person_blockers(person):
    """Why this deceased person cannot be a claimant."""
    blockers = []
    if person.impaired == "yes":
        blockers.append("deceased_impaired")
    if person.at_fault == "yes":
        blockers.append("deceased_at_fault")
    return blockers


def assess(result):
    """{"verdict", "reasons", "explanation", "agent_verdict", "per_person"} for one LeadResult."""
    incident = result.incident
    deceased = _deceased(result)
    reasons, missing = [], []

    if not deceased:
        reasons.append("no_death" if result.people else "insufficient_information")
    if "not_a_road_crash" in result.assessment.reasons:
        reasons.append("not_a_road_crash")
    if incident.crash_type == "single_vehicle" and incident.other_party_involved != "yes":
        reasons.append("single_vehicle_no_other_party")
    elif incident.other_party_involved == "no":
        reasons.append("single_vehicle_no_other_party")

    place = " ".join(filter(None, (incident.location.value, incident.city, incident.county)))
    if place and detect_location(place, residents=False)["location_status"] == OUTSIDE_CALIFORNIA:
        reasons.append("outside_california")
    crash_date = parse_date(incident.date.value)
    if crash_date is None:
        missing.append("crash date")
    if not place:
        missing.append("crash location")

    per_person = []
    for person in deceased:
        blockers = person_blockers(person)
        per_person.append({"name": person.name.value, "can_be_claimant": not blockers, "blockers": blockers})
    if deceased and not any(p["can_be_claimant"] for p in per_person):
        for blocker in ("deceased_impaired", "deceased_at_fault"):
            if all(blocker in p["blockers"] for p in per_person):
                reasons.append(blocker)
        if not any(r in reasons for r in ("deceased_impaired", "deceased_at_fault")):
            reasons.append("deceased_at_fault")  # a mix of impaired and at-fault deceased

    reasons = list(dict.fromkeys(reasons))
    if reasons == ["insufficient_information"] or (not reasons and missing):
        verdict = "needs_review"
        if missing:
            reasons.append("insufficient_information")
    elif reasons:
        verdict = "does_not_qualify"
    elif result.assessment.verdict == "does_not_qualify" and result.assessment.reasons:
        # the agent gives a reason the facts do not show: a person should look
        verdict, reasons = "needs_review", ["insufficient_information"]
    elif incident.other_party_at_fault != "yes":
        verdict = "fault_unidentified"
    else:
        verdict = "qualifies"

    open_items = []
    for person in deceased:
        label = person.name.value or "deceased person"
        if person.at_fault == "unknown" and incident.other_party_at_fault != "yes":
            open_items.append(f"fault not yet reported for {label}")
        if person.impaired == "unknown":
            open_items.append(f"impairment not reported for {label}")

    return {
        "verdict": verdict,
        "reasons": list(dict.fromkeys(reasons)),
        "missing": missing,
        "open_items": open_items if verdict in ("qualifies", "fault_unidentified") else [],
        "explanation": result.assessment.explanation,
        "agent_verdict": result.assessment.verdict,
        "per_person": per_person,
    }
