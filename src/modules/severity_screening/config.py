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
PROMPT_VERSION = "2026-10-20.1"


def _int(name, default):
    value = (os.getenv(name) or "").strip()
    return int(value) if value else default


# JEV connection (.env). TypeSafe's System One API: POST {JEV_API_URL}/systemone with a "state" (the
# articles) and typed "questions"; only the key is required.
JEV_API_URL = (os.getenv("JEV_API_URL") or "https://api.typesafe.ai/v1").strip()
JEV_API_KEY = (os.getenv("JEV_API_KEY") or "").strip()
JEV_MODEL = (os.getenv("JEV_MODEL") or "jev-latest").strip()
JEV_TIMEOUT_SECONDS = _int("JEV_TIMEOUT_SECONDS", 120)
JEV_MAX_RETRIES = _int("JEV_MAX_RETRIES", 4)          # retries after the first attempt (429, 5xx, timeouts)
JEV_LONG_CONTEXT = (os.getenv("JEV_LONG_CONTEXT") or "1").strip() != "0"   # TypeSafe-Danger-Zone-Long-Context header
# An answer below this confidence is treated as "not reported" (choice questions); a yes/no answer
# must be at least this sure either way.
JEV_MIN_CONFIDENCE = float((os.getenv("JEV_MIN_CONFIDENCE") or "0.6").strip())
JEV_MAX_INPUT_CHARS = _int("JEV_MAX_INPUT_CHARS", 0)   # 0 = send the full text of every article

# Statuses that move on to lead_verification.
PASS_ON_STATUSES = ("qualified", "victim_identity_unidentified", "fault_unidentified")
# Fault and other-party answers qualify at a lower confidence; a "no other party" must be this sure to drop a case.
JEV_MIN_CONFIDENCE_FAULT = float((os.getenv("JEV_MIN_CONFIDENCE_FAULT") or "0.5").strip())
JEV_MIN_CONFIDENCE_NO_OTHER_PARTY = float((os.getenv("JEV_MIN_CONFIDENCE_NO_OTHER_PARTY") or "0.8").strip())
JEV_MIN_CONFIDENCE_VICTIM_AT_FAULT = float((os.getenv("JEV_MIN_CONFIDENCE_VICTIM_AT_FAULT") or "0.75").strip())
