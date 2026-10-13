
import json
import os
import re
from typing import List, Literal, Optional

from openai import BadRequestError, OpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator

import config.settings  # noqa: F401 (imported for its .env-loading side effect)


# Only these values are allowed when the source supports a fault assessment.
FaultStatus = Literal[
    "reported_at_fault",
    "alleged_at_fault",
    "not_established",
    "unknown",
]

# What happened to the person. major_injury = serious, severe, critical,
# life-threatening or "major" injuries; minor_injury includes
# "non-life-threatening", "minor" and "moderate" injuries.
OutcomeCategory = Literal[
    "killed",
    "major_injury",
    "minor_injury",
    "injured_severity_unknown",
    "uninjured",
    "unknown",
]

PersonRole = Literal[
    "driver",
    "passenger",
    "pedestrian",
    "cyclist",
    "motorcyclist",
    "victim",
    "other",
    "unknown",
]


# Descriptions and placeholders must never be treated as actual names.
PLACEHOLDER_NAME_PATTERNS = [
    r"^unnamed\b",
    r"^unidentified\b",
    r"^unknown\b",
    r"^anonymous\b",
    r"^name\s+(?:not\s+)?(?:released|provided|available)\b",
    r"^(?:a|an|the)\s+",
    r"^(?:male|female)\s+(?:driver|victim|passenger|pedestrian)\b",
    r"^(?:man|woman|person|driver|victim|passenger|pedestrian|cyclist)\b",
    r"^(?:teen|teenager|child|adult|boy|girl)\b",
    r"^john\s+doe$",
    r"^jane\s+doe$",
    r"\b(?:man|woman|driver|victim|passenger|pedestrian)\s*$",
]


def clean_person_name(value):
    """Return a real reported name, or None for a description/placeholder."""
    if not isinstance(value, str):
        return None

    name = " ".join(value.strip().split())

    if not name:
        return None

    lowered = name.casefold().strip(" .,:;")

    for pattern in PLACEHOLDER_NAME_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return None

    # Reject generic descriptions such as "Bakersfield man".
    if re.fullmatch(
        r"(?:[a-z'-]+\s+){0,4}"
        r"(?:man|woman|person|driver|victim|passenger|pedestrian|cyclist)",
        lowered,
    ):
        return None

    return name


class PersonDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    person_name: Optional[str] = None
    person_age: Optional[int] = None

    person_role: PersonRole = "unknown"
    outcome: Optional[str] = None
    outcome_category: OutcomeCategory = "unknown"
    outcome_evidence: Optional[str] = None

    # True only when the article says this person caused the crash
    # (e.g. the driver who struck the victim, fled, or was arrested).
    caused_crash: Optional[bool] = None

    # Explicit null is accepted. Do not infer fault from involvement alone.
    fault_status: Optional[FaultStatus] = "unknown"
    fault_evidence: Optional[str] = None

    # Only include a relevant public/business address when explicitly reported.
    relevant_address: Optional[str] = None

    @field_validator("person_name", mode="before")
    @classmethod
    def validate_person_name(cls, value):
        return clean_person_name(value)

    @field_validator("outcome_category", mode="before")
    @classmethod
    def validate_outcome_category(cls, value):
        allowed = OutcomeCategory.__args__
        return value if value in allowed else "unknown"

    @field_validator("caused_crash", mode="before")
    @classmethod
    def validate_caused_crash(cls, value):
        return value if isinstance(value, bool) else None

    @field_validator("person_age", mode="before")
    @classmethod
    def validate_person_age(cls, value):
        if value is None or isinstance(value, bool):
            return None

        try:
            age = int(value)
        except (TypeError, ValueError):
            return None

        # Reject implausible or malformed extracted ages.
        if not 0 <= age <= 120:
            return None

        return age


class ArticleAnalysis(BaseModel):
    model_config = ConfigDict(extra="ignore")

    article_title: Optional[str] = None
    source_url: Optional[str] = None

    incident_date: Optional[str] = None
    incident_time: Optional[str] = None
    accident_location: Optional[str] = None
    summary: Optional[str] = None

    people: List[PersonDetails] = Field(default_factory=list)
    extraction_confidence: Literal["high", "medium", "low"] = "low"


# OpenAI models; override with OPENAI_MODEL / OPENAI_FALLBACK_MODEL in .env.
DEFAULT_MODEL = "gpt-4.1-mini"
FALLBACK_MODEL = "gpt-4.1"


def build_llm():
    """Create the OpenAI client (key: OPENAI_API_KEY in the Scraper .env)."""
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY was not found. Check the .env file "
            "in your Scraper project root (write it as OPENAI_API_KEY=sk-...)."
        )

    return OpenAI(
        api_key=api_key,
        max_retries=3,
        timeout=90.0,
    )


def llm_model():
    return (os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL).strip()


# A reply in JSON mode can still be empty or invalid (or rejected with HTTP 400
# "json_validate_failed" by some providers), so the request is retried, then
# sent without JSON mode and the JSON object is taken from the plain-text reply.
JSON_MODE_ATTEMPTS = 2
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)


def _is_json_validate_failed(error):
    return isinstance(error, BadRequestError) and "json_validate_failed" in str(error)


def _parse_json_text(output_text):
    text = (output_text or "").strip()
    # Remove ```json fences if the model added them.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        return json.loads(text)
    except ValueError:
        match = JSON_OBJECT_RE.search(text)
        if not match:
            raise
        return json.loads(match.group(0))


def _request_with_model(client, model, prompt, article_title):
    """JSON mode (retried), then plain text parsed by us. Raises on failure."""
    last_error = None

    for _ in range(JSON_MODE_ATTEMPTS):
        try:
            response = client.responses.create(
                model=model,
                input=prompt,
                text={"format": {"type": "json_object"}},
            )
        except BadRequestError as error:
            if not _is_json_validate_failed(error):
                raise
            last_error = error
            continue

        output_text = (response.output_text or "").strip()
        if output_text:
            try:
                return _parse_json_text(output_text)
            except ValueError as error:
                last_error = error
                continue
        last_error = ValueError(f"{model} returned an empty response for: {article_title}")

    # Last try without JSON mode; the JSON is parsed here.
    response = client.responses.create(model=model, input=prompt)
    output_text = (response.output_text or "").strip()
    if not output_text:
        raise ValueError(
            f"{model} returned no JSON for: {article_title} (last error: {last_error})"
        )
    try:
        return _parse_json_text(output_text)
    except ValueError as error:
        raise ValueError(
            f"{model} reply was not valid JSON for: {article_title}: {output_text[:300]}"
        ) from error


def request_json(client, model, prompt, article_title):
    """Return the model's JSON object.

    If the configured model keeps failing to produce JSON, the same prompt is
    sent to OPENAI_FALLBACK_MODEL in the same call, so the run continues.
    """
    try:
        return _request_with_model(client, model, prompt, article_title)
    except (BadRequestError, ValueError) as error:
        if isinstance(error, BadRequestError) and not _is_json_validate_failed(error):
            raise
        fallback = (os.environ.get("OPENAI_FALLBACK_MODEL") or FALLBACK_MODEL).strip()
        if not fallback or fallback == model:
            raise
        return _request_with_model(client, fallback, prompt, article_title)


def get_article_text(article):
    """Support the text field names used by the extraction pipeline."""
    for key in (
        "extracted_text",
        "article_text",
        "text",
        "content",
        "full_text",
    ):
        value = article.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def analyze_article(article, client):
    """Extract structured accident information from one article."""
    article_title = (
        article.get("article_title")
        or article.get("title")
        or ""
    )
    source_url = (
        article.get("source_url")
        or article.get("url")
        or article.get("link")
        or ""
    )
    article_text = get_article_text(article)

    if not article_text:
        raise ValueError(
            "No article text was found for: "
            + (article_title or source_url or "unknown article")
        )

    published_at = article.get("published_at")

    model = llm_model()

    prompt = f"""
You extract structured facts from California motor-vehicle accident
news reports and official public releases.

Use ONLY the article text below. Do not invent or infer missing facts.

ARTICLE TITLE:
{article_title}

SOURCE URL:
{source_url}

ARTICLE PUBLISHED (UTC, may be missing):
{published_at or "unknown"}

ARTICLE TEXT:
{article_text}

Return one valid JSON object with exactly these fields:

{{
  "article_title": string or null,
  "source_url": string or null,
  "incident_date": string or null,
  "incident_time": string or null,
  "accident_location": string or null,
  "summary": string or null,
  "extraction_confidence": "high" | "medium" | "low",
  "people": [
    {{
      "person_name": string or null,
      "person_age": integer or null,
      "person_role": "driver" | "passenger" | "pedestrian" |
                     "cyclist" | "motorcyclist" | "victim" |
                     "other" | "unknown",
      "outcome": string or null,
      "outcome_category": "killed" | "major_injury" | "minor_injury" |
                          "injured_severity_unknown" | "uninjured" |
                          "unknown",
      "outcome_evidence": string or null,
      "caused_crash": true | false | null,
      "fault_status": "reported_at_fault" | "alleged_at_fault" |
                      "not_established" | "unknown" | null,
      "fault_evidence": string or null,
      "relevant_address": string or null
    }}
  ]
}}

STRICT EXTRACTION RULES:

1. PERSON NAME:
   - Extract an actual person's reported name only.
   - If the article says "an unnamed Bakersfield man",
     "unidentified driver", "unknown victim", or similar,
     person_name MUST be null.
   - Never use a role, city, description, headline phrase,
     or placeholder as a person's name.
   - Do not invent a full name from a first name or description.
   - Preserve the spelling of names as reported.

2. AGE:
   - Use an age only if the article explicitly reports it.
   - Otherwise person_age must be null.

3. INCIDENT DATE:
   - Extract the date the accident occurred, not the article's
     publication date.
   - Use YYYY-MM-DD.
   - When the article gives a weekday or relative day ("Friday",
     "Saturday night", "yesterday") or a month and day without a year
     ("Oct. 8"), resolve it using ARTICLE PUBLISHED: the most recent
     such day on or before the publication date (in California time).
   - If there is no explicit date, no resolvable relative day, or
     ARTICLE PUBLISHED is unknown and the year is not stated,
     incident_date must be null. Do not guess.
   - A date of a later event (arrest, identification, funeral,
     fundraiser, court hearing) is NOT the incident date.

4. LOCATION:
   - Extract the accident location only when supported by the text.
   - Do not confuse a person's residence with the crash location.

5. ROLE AND OUTCOME:
   - Role describes involvement: driver, passenger, pedestrian,
     cyclist, motorcyclist, or another supported role.
   - Outcome describes whether the person was killed, injured,
     uninjured, or whether the outcome is unknown.
   - A driver can also be a victim. Do not confuse role with outcome.
   - outcome_category, from the article's own words about THIS person:
       killed: died, killed, fatal injuries, pronounced dead.
       major_injury: serious, severe, critical, major, grave,
         life-threatening or "great bodily" injuries, critical
         condition, or major injuries that required surgery.
       minor_injury: minor, moderate, "non-life-threatening",
         "non-serious", complaint of pain, treated and released.
       injured_severity_unknown: injured/hospitalized with no severity.
       uninjured: not hurt, uninjured, unharmed.
       unknown: the article does not say.
     "Non-life-threatening" is minor_injury, never major_injury.
   - outcome_evidence: the short phrase from the article that supports
     outcome_category, or null.

5b. WHO CAUSED THE CRASH:
   - caused_crash is true only when the article says this person caused
     the crash: e.g. the driver who struck the victim, ran a red light,
     was driving under the influence, fled the scene, or was arrested
     or cited for the crash.
   - caused_crash is false for a person who was struck or hit by
     another driver/vehicle, or explicitly not at fault.
   - Otherwise caused_crash is null.
   - If the article says or suggests THIS person was speeding, impaired,
     ran a light/stop sign, crossed the center line, or lost control on
     their own, set fault_status to "alleged_at_fault" (or
     "reported_at_fault" when officials state it) and put the phrase in
     fault_evidence. In that case caused_crash must not be false.
   - fault_status describes THIS person only. A pedestrian, cyclist or
     driver who was struck by someone else is not at fault because the
     other driver is at fault.

6. FAULT:
   - Use reported_at_fault only for an explicit established finding.
   - Use alleged_at_fault for an allegation or suspected responsibility.
   - Use not_established when the article explicitly says fault
     has not been established.
   - Use unknown when the article provides no reliable assessment.
   - Never infer fault simply because someone was driving or involved.
   - If uncertain, use "unknown", not null.

7. MISSING DATA:
   - Use null for unknown dates, times, ages, names, locations,
     summaries, evidence, outcomes, or addresses.
   - Never fill gaps using outside knowledge.
   - Do not infer or search for private home addresses.
   - relevant_address may contain only a relevant public/business
     address explicitly reported in the article; otherwise null.

8. PEOPLE:
   - Include people discussed in connection with the incident.
   - A person with no reported name may still be represented, but
     person_name must be null.
   - Do not duplicate the same person within the article.

9. OUTPUT:
   - Return JSON only. No Markdown fences or commentary.
   - Use valid JSON null, not the string "null".
   - Every field must follow the schema above.
"""

    # Parse JSON, then normalize placeholder names before validation.
    # This also handles responses where the model returns null fault_status.
    data = request_json(client, model, prompt, article_title)

    if not isinstance(data, dict):
        raise ValueError("The model response must be a JSON object.")

    raw_people = data.get("people")
    if isinstance(raw_people, list):
        for person in raw_people:
            if not isinstance(person, dict):
                continue

            if "person_name" in person:
                person["person_name"] = clean_person_name(
                    person["person_name"]
                )

    # Pydantic validates the structure and allowed field values.
    result = ArticleAnalysis.model_validate(data)

    # Use the known input metadata when the model omits it.
    if not result.article_title:
        result.article_title = article_title or None

    if not result.source_url:
        result.source_url = source_url or None

    return result
