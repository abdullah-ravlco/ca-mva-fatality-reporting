"""OpenAI client and JSON request helper shared by the LLM stages."""

import json
import os
import re

from openai import BadRequestError, OpenAI

import config.settings  # noqa: F401 (imported for its .env-loading side effect)


# OpenAI models; override with OPENAI_MODEL / OPENAI_FALLBACK_MODEL in .env.
DEFAULT_MODEL = "gpt-4.1-mini"
FALLBACK_MODEL = "gpt-4.1"


def build_llm():
    """Create the OpenAI client (key: OPENAI_API_KEY in the project .env)."""
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY was not found. Check the .env file "
            "in the project root (write it as OPENAI_API_KEY=sk-...)."
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
