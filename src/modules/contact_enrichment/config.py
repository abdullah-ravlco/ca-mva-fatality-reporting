"""Settings for contact enrichment (SearchBug)."""

import os

from config.settings import DATA_DIR, module_data_dir

MODULE = "contact_enrichment"
LEADS_FILE = module_data_dir("lead_verification") / "verified_leads.json"
PASSED_LEADS_FILE = module_data_dir("severity_screening") / "passed_leads.json"   # article text, for relationships
OUTPUT_NAME = "contacts.json"
CSV_NAME = "contacts.csv"            # the same, one row per relative, for spreadsheets
UNNAMED_CSV_NAME = "unnamed_people.csv"   # deceased people whose name is not reported: nothing to look up
DETAILS_NAME = "contacts_details.json"
ERRORS_NAME = "searchbug_errors.json"
OUTPUT_FILE = module_data_dir(MODULE) / OUTPUT_NAME
DETAILS_FILE = module_data_dir(MODULE) / DETAILS_NAME
CACHE_FILE = DATA_DIR / "state" / "searchbug_cache.json"   # every response, so nothing is paid for twice


def _int(name, default):
    value = (os.getenv(name) or "").strip()
    return int(value) if value else default


# SearchBug Enhanced People Search (TYPE=api_ppl), form POST.
SEARCHBUG_API_URL = (os.getenv("SEARCHBUG_API_URL") or "https://data.searchbug.com/api/search.aspx").strip()
SEARCHBUG_CO_CODE = (os.getenv("SEARCHBUG_CO_CODE") or "").strip()      # account number
SEARCHBUG_API_KEY = (os.getenv("SEARCHBUG_API_KEY") or "").strip()      # API key (sent as PASS)
SEARCHBUG_TIMEOUT_SECONDS = _int("SEARCHBUG_TIMEOUT_SECONDS", 60)
SEARCHBUG_MAX_RETRIES = _int("SEARCHBUG_MAX_RETRIES", 3)
SEARCHBUG_RESULT_LIMIT = _int("SEARCHBUG_RESULT_LIMIT", 25)           # LIMIT: candidates per search (max 250)
# Paid lookups (not answered from the cache) allowed in one run; the rest wait for the next run.
MAX_LOOKUPS_PER_RUN = _int("SEARCHBUG_MAX_LOOKUPS_PER_RUN", 100)
# Next of kin per deceased person: the closest relatives (spouse, children, parents, siblings, others),
# looked up and listed. The second is left empty when there is only one.
MAX_RELATIVES = _int("SEARCHBUG_MAX_RELATIVES", 2)
# Phones listed per person (deceased and each next of kin): the most recently seen ones.
MAX_PHONES = _int("SEARCHBUG_MAX_PHONES", 2)
REFERENCE = "ca-mva-fatality-reporting"

# Google Sheet that receives new contacts through an Apps Script web app (see sheets.py). URL and token are needed.
GOOGLE_SCRIPT_URL = (os.getenv("GOOGLE_SCRIPT_URL") or "").strip()
GOOGLE_SCRIPT_TOKEN = (os.getenv("GOOGLE_SCRIPT_TOKEN") or "").strip()
GOOGLE_SHEET_TAB = (os.getenv("GOOGLE_SHEET_TAB") or "").strip()   # an existing tab; empty = the first tab

# Which lead_verification verdicts are looked up.
VERDICTS_TO_ENRICH = ("qualifies", "fault_unidentified")

# A candidate matches the deceased person when the names agree AND the age or a death record agrees.
AGE_TOLERANCE_YEARS = 1
DEATH_DATE_TOLERANCE_DAYS = 45
