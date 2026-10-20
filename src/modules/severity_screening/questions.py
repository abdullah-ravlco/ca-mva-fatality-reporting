"""The questions asked of JEV for one incident, and how its answers become the screening facts.

JEV classifies the articles; it cannot quote them or pull out names. So one incident is screened as
one "victim" (the person killed or injured), and:
  - injury, other party, fault, impairment and conflicts come from JEV's answers;
  - the crash date, location and age come from the incident grouping (incident_grouping);
  - the victim's name is not extracted: JEV only says whether a name is given, and lead_verification
    looks the name up.
An answer JEV is not sure about (confidence below JEV_MIN_CONFIDENCE) counts as "not reported", so it
can neither qualify nor drop a case.
"""

from src.modules.severity_screening import config
from src.modules.severity_screening.decision import decide
from src.modules.severity_screening.schema import JevResult

NAMED_IN_ARTICLES = "(named in articles)"

QUESTIONS = {
    "injury": {
        "type": "choice",
        "instructions": "What is the worst outcome reported for a person killed or injured in this crash?",
        "criteria": {
            "death": "A person died because of the crash (at the scene, in hospital, brain death, cardiac arrest).",
            "catastrophic": (
                "No death reported, but a catastrophic injury: spinal cord injury or paralysis, traumatic brain "
                "injury, skull fracture, multiple fractures, amputation, severe burns, loss of vision or hearing, "
                "organ damage, life-threatening internal bleeding, permanent disability, or 'critical condition' "
                "or 'life-threatening injuries'."
            ),
            "serious": (
                "Serious but not catastrophic: broken bones, fractures needing surgery, collapsed lung, "
                "non-life-threatening internal bleeding, nerve damage."
            ),
            "minor_or_moderate": "Concussion, whiplash, sprains, bruises, cuts, soreness, minor fractures.",
            "not_reported": "Someone is 'injured', 'seriously injured' or 'hospitalized' with no detail on the injury.",
            "no_injury": "Nobody was killed or injured.",
        },
    },
    "other_party": {
        "type": "choice",
        "instructions": (
            "Was anyone other than the victims and the driver of a victim's own vehicle involved in the crash "
            "(another vehicle, or a driver who struck a pedestrian or cyclist)?"
        ),
        "criteria": {
            "yes": "Another vehicle or driver is confirmed to be involved.",
            "no": "The articles establish nobody else was involved (a solo crash, a rollover with no other vehicle).",
            "unclear": "Not reported or ambiguous. An unmentioned other party is 'unclear', not 'no'.",
        },
    },
    "fault": {
        "type": "choice",
        "instructions": "According to the articles, who caused the crash?",
        "criteria": {
            "other_party": (
                "Another driver or vehicle caused it, and the articles say what they did (ran a red light, "
                "speeding, DUI, wrong-way, arrested or cited)."
            ),
            "victim": "The person killed or injured caused the crash themself.",
            "own_driver": "The driver of the vehicle a passenger victim rode in caused the crash.",
            "not_established": (
                "Not reported, 'under investigation', a medical emergency, or the only evidence is that the "
                "victim was struck."
            ),
        },
    },
    "impaired": {
        "type": "noul",
        "instructions": (
            "Was a person who was killed or injured reported to have been impaired by alcohol or drugs while "
            "driving or riding in this crash (not the other driver)?"
        ),
    },
    "conflict": {
        "type": "noul",
        "instructions": (
            "Do the articles contradict each other on an important fact (the number of people killed, who the "
            "victim is, the crash date, or who was at fault)?"
        ),
    },
    "victim_named": {
        "type": "noul",
        "instructions": "Does an article give the full name of a person who was killed or injured?",
    },
}

INJURY = {  # answer -> (outcome, injury_tier)
    "death": ("killed", "catastrophic"),
    "catastrophic": ("injured", "catastrophic"),
    "serious": ("injured", "serious"),
    "minor_or_moderate": ("injured", "moderate"),
    "not_reported": ("unknown", "unknown"),
}
FAULT = {"other_party": "other_party", "victim": "this_victim", "own_driver": "own_driver"}


def pick(answer, minimum=None):
    """A choice answer's option, or None when JEV is not confident enough."""
    minimum = config.JEV_MIN_CONFIDENCE if minimum is None else minimum
    if not answer or (answer.get("confidence") or 0) < minimum:
        return None
    return answer.get("choice")


def yes_no(answer, minimum=None):
    """'yes' / 'no' from a noul answer (probability of yes), or 'unknown' when it is in between."""
    minimum = config.JEV_MIN_CONFIDENCE if minimum is None else minimum
    p = (answer or {}).get("noul")
    if not isinstance(p, (int, float)):
        return "unknown"
    return "yes" if p >= minimum else "no" if p <= 1 - minimum else "unknown"


def crash_date(incident):
    for candidate in incident.get("candidate_incident_dates") or []:
        if candidate.get("date"):
            return candidate["date"]
    return None


def crash_location(incident):
    for location in incident.get("locations") or []:
        text = ", ".join(p for p in (location.get("road"), location.get("place"), location.get("county")) if p)
        if text or location.get("location_text"):
            return text or location["location_text"]
    return None


def to_result(answers, incident):
    """The JevResult (the facts decision.py judges) for an incident, from JEV's answers."""
    injury = pick(answers.get("injury"))
    party = pick(answers.get("other_party"))
    fault = pick(answers.get("fault"))
    impaired = yes_no(answers.get("impaired"))
    named = yes_no(answers.get("victim_named")) == "yes"
    ages = incident.get("victim_ages_mentioned") or []
    notes = [f"{name}: {answers[name].get('choice') or answers[name].get('noul')} "
             f"(confidence {answers[name].get('confidence', 'n/a')})" for name in answers]
    if len(ages) > 1:
        notes.append(f"several ages mentioned {ages}; the first was used")

    victims = []
    if injury != "no_injury":
        outcome, tier = INJURY.get(injury, ("unknown", "unknown"))
        victims.append({
            "name": NAMED_IN_ARTICLES if named else None,
            "age": ages[0] if ages else None,
            "role": "driver" if impaired == "yes" else "unknown",
            "outcome": outcome, "injury_tier": tier,
            "at_fault_party": FAULT.get(fault, "unknown"),
            "fault_basis": "described_in_article" if fault in FAULT else "not_reported",
            "impaired": impaired,
            "evidence": "classified",
        })

    result = JevResult.model_validate({
        "crash_date": crash_date(incident),
        "crash_location": crash_location(incident),
        "victims": victims,
        "other_party": {"involved": party if party in ("yes", "no") else "unclear"},
        "conflicts": ["the articles contradict each other (JEV)"] if yes_no(answers.get("conflict")) == "yes" else [],
        "notes": "; ".join(notes),
    })
    result.case_status = decide(result)["status"]  # JEV proposes no status; the decision rules do
    return result
