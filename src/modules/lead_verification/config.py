"""Settings for step 4: analysing each grouped incident against its sources."""

import re

from config.settings import module_data_dir

# Input from the incident_grouping step.
INCIDENTS_FILE = module_data_dir("incident_grouping") / "incidents.json"

# Outputs.
MODULE = "lead_verification"
DATA_DIR = module_data_dir(MODULE)
OUTPUT_NAME = "verified_leads.json"
OUTPUT_FILE = DATA_DIR / OUTPUT_NAME
FETCH_CACHE_FILE = DATA_DIR / "fetch_cache.json"

# The agent (agent.py): per incident, how many model calls (tool round trips) and web searches.
MAX_AGENT_STEPS = 8
MAX_WEB_SEARCHES_PER_INCIDENT = 6
# Article text given to the model, per article / per incident, and per page it fetches itself.
MAX_SOURCE_CHARS = 20000
MAX_SOURCES_PER_PROMPT = 8
MAX_FETCHED_PAGE_CHARS = 20000
MAX_CACHED_TEXT_CHARS = 100000

# Official sources: one of these confirms a fact on its own.
OFFICIAL_HOST_RE = re.compile(
    r"(\.gov$|\.gov\.|riversidesheriff\.org|sheriff|police|coroner|\bchp\b)", re.I
)
# Law-firm sites repost news; they never count as an independent confirmation.
LAW_FIRM_HOST_RE = re.compile(r"law|legal|attorney|lawyer|injury", re.I)

# Never used as sources: people-search, background-check and property sites.
BLOCKED_HOST_RE = re.compile(
    r"whitepages|spokeo|beenverified|truepeoplesearch|fastpeoplesearch|radaris|intelius|"
    r"peoplefinders|peoplelooker|mylife|truthfinder|instantcheckmate|clustrmaps|"
    r"zillow|redfin|realtor\.com|trulia|publicrecords|backgroundcheck|addresses\.com|"
    r"facebook\.com|instagram\.com|tiktok\.com|x\.com|twitter\.com",
    re.I,
)

REQUEST_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
