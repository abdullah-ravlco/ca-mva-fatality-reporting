"""Catch model mistakes the rules depend on, before the verdict is decided.

Each check fixes the result in place and returns a note of what it changed, so every correction is
visible in the output. A correction only ever removes a claim the quotes do not support (or a
reason the facts contradict); it never adds a fact.

  1. "not_a_road_crash" is dropped when the facts describe a vehicle crash (a vehicle struck someone
     or another vehicle, or another party was involved).
  2. A fault basis of "cited or arrested" needs a quote that mentions a citation, arrest or charge.
  3. "The other party was at fault" needs a quote that shows it; a quote that only says the crash is
     under investigation or the driver had a medical emergency is not enough.
  4. "This person was at fault" needs a quote; otherwise it becomes unknown.
  5. A residence needs a quote that says where the person lived (not where the crash was).
"""

import re

CITED_RE = re.compile(r"\b(cit(e|ed|ation)|arrest(ed)?|charg(e|ed)|booked|custody|jail(ed)?|indict(ed)?)\b", re.I)
FAULT_ACT_RE = re.compile(
    r"\b(speed(ing)?|ran (a|the) (red|stop)|red light|stop sign|lost control|fail(ed|ure)? to yield|"
    r"wrong[- ]way|dui|drunk|impaired|under the influence|reckless|crossed (into|over|the)|"
    r"rear[- ]ended|ran (over|through|into)|at fault|caused the (crash|collision)|"
    r"cit(e|ed|ation)|arrest(ed)?|charg(e|ed)|booked|hit[- ]and[- ]run|fled)\b",
    re.I,
)
UNCERTAIN_RE = re.compile(r"\b(under investigation|medical emergency|unknown|unclear|being investigated)\b", re.I)
VEHICLE_CRASH_TYPES = ("multi_vehicle", "pedestrian_or_cyclist_struck")
RESIDENCE_CUES = r"(?:of|from|resident of|resident|lived in|lives in|living in|native of|home in)"


def _vehicle_crash(result):
    return result.incident.crash_type in VEHICLE_CRASH_TYPES or result.incident.other_party_involved == "yes"


def check_road_crash(result):
    if "not_a_road_crash" in result.assessment.reasons and _vehicle_crash(result):
        result.assessment.reasons = [r for r in result.assessment.reasons if r != "not_a_road_crash"]
        return ["ignored 'not_a_road_crash': the facts describe a vehicle crash"]
    return []


def check_cited_basis(holder, basis_field, quote_field, label):
    basis, quote = getattr(holder, basis_field), getattr(holder, quote_field)
    if basis in ("other_party_cited_or_arrested", "deceased_cited_or_arrested") and not (quote and CITED_RE.search(quote)):
        setattr(holder, basis_field, "circumstances_described" if quote else "none")
        return [f"{label}: fault basis '{basis}' not supported by the quote (no citation or arrest mentioned)"]
    return []


def check_other_party_fault(result):
    incident = result.incident
    notes = check_cited_basis(incident, "other_party_fault_basis", "other_party_fault_quote", "other party")
    if incident.other_party_at_fault == "yes":
        quote = incident.other_party_fault_quote or ""
        if not FAULT_ACT_RE.search(quote):
            reason = "the quote only says the cause is uncertain" if UNCERTAIN_RE.search(quote) else (
                "no quote shows what the other party did" if quote else "no quote")
            incident.other_party_at_fault = "unknown"
            incident.other_party_fault_basis = "none"
            notes.append(f"other party at fault -> unknown: {reason}")
    return notes


def check_person(person):
    label = person.name.value or "unnamed person"
    notes = check_cited_basis(person, "fault_basis", "fault_quote", label)
    if person.at_fault == "yes" and not person.fault_quote:
        person.at_fault, person.fault_basis = "unknown", "none"
        notes.append(f"{label}: at fault -> unknown (no quote)")
    if person.impaired == "yes" and not person.impaired_quote:
        person.impaired = "unknown"
        notes.append(f"{label}: impaired -> unknown (no quote)")
    address = person.address
    if address.value:
        pattern = rf"(\b{RESIDENCE_CUES}\s+{re.escape(address.value)}\b|\b{re.escape(address.value)}\s+(?:man|woman|resident|teen|boy|girl|mother|father|native)\b)"
        if not (address.quote and re.search(pattern, address.quote, re.I)):
            notes.append(f"{label}: residence '{address.value}' removed (the quote does not say they lived there)")
            address.value = address.quote = address.source_url = None
    return notes


def apply_checks(result):
    """Fix unsupported claims in a LeadResult (in place). Returns the list of corrections."""
    notes = check_road_crash(result) + check_other_party_fault(result)
    for person in result.people:
        notes += check_person(person)
    return notes
