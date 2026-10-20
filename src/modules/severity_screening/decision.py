"""The case status, decided in Python from the facts JEV's answers were mapped to (questions.py).

JEV proposes a status too, but this one is used: the same facts always give the same status, and
every dropped case says why. A disagreement with JEV is recorded (status_mismatch).

Each victim is judged on its own and the incident takes the best result, so a passenger can qualify
even when a driver in the same crash was drunk. An incident is dropped only when every victim is.

  qualified                     - catastrophic, another party involved and at fault, victim identified
  victim_identity_unidentified  - all else qualifies, the victim's name is not reported
  fault_unidentified            - all else qualifies (name, age, date, location), fault unknown
  dropped                       - with one or more reason codes (DROP_REASONS):
      disqualified by the evidence: injury_not_catastrophic, no_other_party, victim_at_fault,
        own_driver_at_fault, victim_impaired_driver
      not enough to qualify: injury_not_reported, other_party_unclear, conflicting_reports,
        crash_details_missing, age_missing, name_and_fault_unknown, no_victim_reported
"""

from src.modules.lead_verification.checks import FAULT_ACT_RE

QUALIFIED = "qualified"
IDENTITY = "victim_identity_unidentified"
FAULT = "fault_unidentified"
DROPPED = "dropped"

# Best first: the incident takes the best status any victim reaches.
RANK = [QUALIFIED, FAULT, IDENTITY, DROPPED]
SELF_PROPELLED_ROLES = ("driver", "motorcyclist", "cyclist")

# Why a victim was dropped: code -> plain-language reason.
DROP_REASONS = {
    # The evidence rules the case out.
    "injury_not_catastrophic": "the reported injury is not catastrophic",
    "no_other_party": "no other party was involved",
    "victim_at_fault": "the victim caused the crash",
    "own_driver_at_fault": "the driver of the vehicle this passenger rode in caused the crash",
    "victim_impaired_driver": "the victim was driving or riding impaired",
    # Not enough information to qualify the case.
    "injury_not_reported": "the injury severity is not reported",
    "other_party_unclear": "it is not reported whether another party was involved",
    "conflicting_reports": "the articles contradict each other",
    "crash_details_missing": "the crash date or location is not reported",
    "age_missing": "the victim's age is not reported",
    "name_and_fault_unknown": "both the victim's name and who was at fault are unknown",
    "no_victim_reported": "no killed or injured person is reported",
}


def is_catastrophic(victim):
    return victim.outcome == "killed" or victim.injury_tier == "catastrophic"


def drop(*codes, detail=None):
    """(DROPPED, codes, reasons) - detail replaces the generic text of the first code."""
    reasons = [DROP_REASONS[code] for code in codes]
    if detail:
        reasons[0] = detail
    return DROPPED, list(codes), reasons


def supported_claims(victim):
    """(at_fault_party, impaired, corrections): fault and impairment claims JEV could not quote are
    treated as unknown, so an unsupported claim can neither qualify nor drop a case. "The other party
    was at fault" also needs a quote that shows what they did: "under investigation" or "the driver
    had a medical emergency" is not enough."""
    at_fault, impaired, notes = victim.at_fault_party, victim.impaired, []
    if victim.evidence == "classified":
        return at_fault, impaired, notes
    label = victim.name or "unnamed victim"
    quote = victim.fault_quote or ""
    if at_fault != "unknown" and not quote:
        notes.append(f"{label}: fault '{at_fault}' ignored (no quote)")
        at_fault = "unknown"
    elif at_fault == "other_party" and not FAULT_ACT_RE.search(quote):
        notes.append(f"{label}: other party's fault ignored (the quote does not show what they did)")
        at_fault = "unknown"
    if impaired == "yes" and not victim.impaired_quote:
        notes.append(f"{label}: impairment ignored (no quote)")
        impaired = "unknown"
    return at_fault, impaired, notes


def judge_victim(victim, result):
    """(status, tags, reasons) for one victim. For a dropped victim, tags are the drop codes."""
    other = result.other_party.involved
    at_fault, impaired, _ = supported_claims(victim)

    # The evidence rules the case out.
    if not is_catastrophic(victim) and victim.injury_tier in ("serious", "moderate", "minor"):
        return drop("injury_not_catastrophic", detail=f"the reported injury is {victim.injury_tier}, not catastrophic")
    if other == "no":
        return drop("no_other_party")
    if at_fault == "this_victim":
        return drop("victim_at_fault")
    if at_fault == "own_driver":
        return drop("own_driver_at_fault")
    if impaired == "yes" and victim.role in SELF_PROPELLED_ROLES:
        return drop("victim_impaired_driver", detail=f"the victim was an impaired {victim.role}")

    # Not enough information to qualify: every missing point is listed.
    name_missing = not victim.name
    fault_missing = at_fault == "unknown"
    missing = []
    if not is_catastrophic(victim):
        missing.append("injury_not_reported")
    if other == "unclear":
        missing.append("other_party_unclear")
    if result.conflicts:
        missing.append("conflicting_reports")
    if not result.crash_date or not result.crash_location:
        missing.append("crash_details_missing")
    if victim.name and victim.age is None:
        missing.append("age_missing")
    if name_missing and fault_missing:
        missing.append("name_and_fault_unknown")
    if missing:
        status, codes, reasons = drop(*missing)
        if "conflicting_reports" in codes:
            reasons[codes.index("conflicting_reports")] += ": " + "; ".join(result.conflicts)
        return status, codes, reasons

    if name_missing:
        return IDENTITY, [IDENTITY], ["qualifies except that the victim's name is not reported"]
    if fault_missing:
        return FAULT, [FAULT], ["qualifies except that fault is not established"]
    return QUALIFIED, [], ["catastrophic injury, another party involved and at fault, victim identified"]


def decide(result):
    """{"status", "drop_reason", "tags", "reasons", "per_victim", "jev_status", "status_mismatch"}."""
    per_victim = []
    for victim in result.victims:
        status, tags, reasons = judge_victim(victim, result)
        per_victim.append({"name": victim.name, "status": status, "tags": tags, "reasons": reasons})

    if per_victim:
        best = min(per_victim, key=lambda v: RANK.index(v["status"]))
        status, tags, reasons = best["status"], best["tags"], best["reasons"]
    else:
        status, tags, reasons = drop("no_victim_reported")

    drop_reason = None
    if status == DROPPED:
        codes = sorted({code for v in per_victim for code in v["tags"]} or set(tags))
        if len(per_victim) > 1:  # name each victim's reasons
            text = "; ".join(f"{v['name'] or 'unnamed victim'}: {', '.join(v['reasons'])}" for v in per_victim)
        else:
            text = ", ".join(reasons)
        drop_reason = {"codes": codes, "text": text}

    return {
        "status": status,
        "drop_reason": drop_reason,
        "tags": sorted(set(tags)),
        "reasons": reasons,
        "per_victim": per_victim,
        "corrections": [note for victim in result.victims for note in supported_claims(victim)[2]],
        "jev_status": result.case_status,
        "jev_tags": result.tags,
        "status_mismatch": result.case_status != status,
    }
