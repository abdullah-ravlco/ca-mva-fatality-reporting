"""Decide which SearchBug record (if any) is the deceased person, and name relatives' relationships.

A candidate is accepted only when its names agree with the deceased's name AND either its age agrees
(from a date of birth, within AGE_TOLERANCE_YEARS of the reported age at the crash date) or it has a
death record near the crash (or SearchBug's deceased flag). An accepted candidate is "strong" when it
also lived in a reported city or has a death record; otherwise it is "weak" (name and age only).

  exactly one strong candidate              -> matched
  several strong candidates                 -> ambiguous   (a person chooses; never picked)
  no strong, exactly one weak candidate     -> possible_match (a person confirms; relatives not fetched)
  no strong, several weak candidates        -> ambiguous
  none                                      -> not_found
"""

import re
from datetime import date

from src.modules.contact_enrichment.config import AGE_TOLERANCE_YEARS, DEATH_DATE_TOLERANCE_DAYS

MATCHED, AMBIGUOUS, POSSIBLE, NOT_FOUND = "matched", "ambiguous", "possible_match", "not_found"


def _tokens(name):
    return [t for t in re.sub(r"[^a-z ]", " ", (name or "").lower()).split() if len(t) > 1]


def name_matches(target, candidate_names):
    """First and last name of the target appear in one of the candidate's names."""
    wanted = _tokens(target)
    if len(wanted) < 2:
        return False
    first, last = wanted[0], wanted[-1]
    return any(first in _tokens(n) and last in _tokens(n) for n in candidate_names)


def _parse(value):
    if not value:
        return None
    parts = [int(p) for p in str(value).split("-") if p.isdigit()]
    if not parts:
        return None
    return date(parts[0], parts[1] if len(parts) > 1 else 6, parts[2] if len(parts) > 2 else 15)


def age_matches(age, crash_date, candidate):
    if age is None:
        return False
    for dob in candidate["dobs"]:
        born = _parse(dob["date"])
        if born and crash_date:
            years = (crash_date - born).days / 365.25
            if abs(years - age) <= AGE_TOLERANCE_YEARS + 0.5:
                return True
    for dod in candidate["dods"]:
        if dod["age_at_death"] is not None and abs(dod["age_at_death"] - age) <= AGE_TOLERANCE_YEARS:
            return True
    return False


def death_record_matches(crash_date, candidate):
    if candidate.get("deceased_flag") and not candidate["dods"]:
        return True  # SearchBug marks the person deceased but gives no date
    for dod in candidate["dods"]:
        died = _parse(dod["date"])
        if died and crash_date and 0 <= (died - crash_date).days <= DEATH_DATE_TOLERANCE_DAYS:
            return True
    return False


def score(target, candidate):
    """(accepted, reasons) for one candidate. "strong" is added to reasons for a strong match."""
    crash = _parse(target.get("crash_date"))
    reasons = []
    if not name_matches(target["name"], candidate["names"]):
        return False, ["name does not match"]
    reasons.append("name matches")
    by_age = age_matches(target.get("age"), crash, candidate)
    by_death = death_record_matches(crash, candidate)
    if by_age:
        reasons.append("age matches")
    if by_death:
        reasons.append("death record near the crash date")
    cities = {c.lower() for c in target.get("cities") or [] if c}
    by_city = bool(cities) and any((a["city"] or "").lower() in cities for a in candidate["addresses"])
    if by_city:
        reasons.append("lived in a reported city")
    years = {d["date"][:4] for d in candidate["dobs"] if d["date"]}
    if len(years) > 1:
        reasons.append(f"conflicting dates of birth ({', '.join(sorted(years))})")
    accepted = by_age or by_death
    if accepted and (by_city or by_death):
        reasons.append("strong")
    return accepted, reasons


def choose(target, candidates):
    """(status, chosen_candidate_or_None, scored) with scored = [(candidate, accepted, reasons)]."""
    scored = [(c, *score(target, c)) for c in candidates]
    strong = [c for c, ok, reasons in scored if ok and "strong" in reasons]
    weak = [c for c, ok, reasons in scored if ok and "strong" not in reasons]
    if len(strong) == 1:
        return MATCHED, strong[0], scored
    if len(strong) > 1:
        return AMBIGUOUS, None, scored
    if len(weak) == 1:
        return POSSIBLE, weak[0], scored
    if len(weak) > 1:
        return AMBIGUOUS, None, scored
    return NOT_FOUND, None, scored


RELATION_WORDS = [
    "husband", "wife", "spouse", "partner", "fiance", "fiancee", "fiancé", "fiancée", "boyfriend", "girlfriend",
    "son", "daughter", "stepson", "stepdaughter", "mother", "father", "mom", "dad", "stepmother", "stepfather",
    "brother", "sister", "grandmother", "grandfather", "grandson", "granddaughter", "aunt", "uncle",
    "cousin", "niece", "nephew",
]


def relationship_from_articles(relative_names, texts):
    """(relationship, quote) when an article puts a relationship word right before this relative's
    name, e.g. "She is survived by her husband, John Lopez" -> ("husband", that sentence).

    The relationship word must directly precede the first name (only ", " or " " between them), and
    the relative's last name must follow it, so a word elsewhere in the sentence never counts.
    Only the article's own words are used; nothing is guessed.
    """
    for full in relative_names:
        tokens = _tokens(full)
        if len(tokens) < 2:
            continue
        first, last = re.escape(tokens[0]), re.escape(tokens[-1])
        pattern = re.compile(rf"\b({'|'.join(RELATION_WORDS)})s?,?\s+{first}\b[\w .'-]{{0,25}}?\b{last}\b", re.I)
        for text in texts:
            match = pattern.search(text)
            if match:
                start = text.rfind(".", 0, match.start()) + 1
                end = text.find(".", match.end())
                quote = text[start:(end + 1) if end != -1 else None].strip()
                return match.group(1).lower(), quote
    return None, None
