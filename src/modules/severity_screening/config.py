"""Settings for the severity-screening stage (JEV)."""

import os

from config.settings import module_data_dir

MODULE = "severity_screening"
INCIDENTS_FILE = module_data_dir("incident_grouping") / "incidents.json"
OUTPUT_NAME = "screened_incidents.json"
ERRORS_NAME = "screening_errors.json"
OUTPUT_FILE = module_data_dir(MODULE) / OUTPUT_NAME
# The leads that passed screening, with the full content of their articles: lead_verification's input.
PASSED_LEADS_NAME = "passed_leads.json"
PASSED_LEADS_FILE = module_data_dir(MODULE) / PASSED_LEADS_NAME

# Bump when the prompt or the decision rules change, so every incident is screened again.
PROMPT_VERSION = "2026-10-19.2"


def _int(name, default):
    value = (os.getenv(name) or "").strip()
    return int(value) if value else default


# JEV connection (.env). The API is called in the OpenAI chat-completions format:
# POST {JEV_API_URL}/chat/completions with {"model", "messages"}.
JEV_API_URL = (os.getenv("JEV_API_URL") or "").strip()
JEV_API_KEY = (os.getenv("JEV_API_KEY") or "").strip()
JEV_MODEL = (os.getenv("JEV_MODEL") or "").strip()
JEV_TIMEOUT_SECONDS = _int("JEV_TIMEOUT_SECONDS", 120)
JEV_MAX_RETRIES = _int("JEV_MAX_RETRIES", 4)          # retries after the first attempt (429, 5xx, timeouts)
JEV_JSON_MODE = (os.getenv("JEV_JSON_MODE") or "1").strip() != "0"   # ask for response_format=json_object
JEV_MAX_INPUT_CHARS = _int("JEV_MAX_INPUT_CHARS", 0)   # 0 = send the full text of every article

# Statuses that move on to lead_verification.
PASS_ON_STATUSES = ("qualified", "victim_identity_unidentified", "fault_unidentified")
