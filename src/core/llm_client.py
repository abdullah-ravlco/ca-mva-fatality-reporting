"""OpenAI client and structured-output request helper shared by the LLM stages."""

import os

from openai import OpenAI
from pydantic import ValidationError

import config.settings  # noqa: F401 (imported for its .env-loading side effect)


# OpenAI model; override with OPENAI_MODEL in .env. It must support structured outputs.
DEFAULT_MODEL = "gpt-4.1-mini"


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


def request_structured(client, model, prompt, schema, label):
    """Return the reply as an instance of the pydantic model `schema`.

    Structured outputs constrain the reply to the schema, so it is never
    malformed; it can still be cut off or refused, which raises ValueError.
    """
    try:
        response = client.responses.parse(model=model, input=prompt, text_format=schema)
    except ValidationError as error:
        # The SDK parses the reply itself; a reply cut off at the token limit
        # fails here, before the response status can be checked.
        raise ValueError(f"{model} reply was cut off or invalid for: {label}") from error
    if response.status == "incomplete":
        reason = getattr(response.incomplete_details, "reason", None)
        raise ValueError(f"{model} reply was incomplete ({reason}) for: {label}")
    if response.output_parsed is None:
        raise ValueError(f"{model} returned no structured reply (refused?) for: {label}")
    return response.output_parsed
