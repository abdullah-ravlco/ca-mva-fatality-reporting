"""Settings for step 4: verifying the shortlisted leads from article_analysis."""

import re

from config.settings import module_data_dir

# Inputs from earlier steps.
SHORTLIST_FILE = module_data_dir("article_analysis") / "shortlisted_articles.json"
EXTRACTED_TEXT_FILE = module_data_dir("link_extraction") / "extracted_text.json"

# Outputs.
DATA_DIR = module_data_dir("lead_verification")
OUTPUT_FILE = DATA_DIR / "verified_leads.json"
FETCH_CACHE_FILE = module_data_dir("lead_verification") / "fetch_cache.json"

# A lead whose verified incident date is older than this is rejected.
MAX_DAYS = 10

# Follow-up search (Google News RSS), per incident.
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
SEARCH_WINDOW_DAYS = 30
MAX_QUERIES_PER_INCIDENT = 4
MAX_RESULTS_PER_QUERY = 8
MAX_FETCHES_PER_INCIDENT = 10
MAX_NEW_SOURCES_PER_INCIDENT = 5

# LLM web search (OpenAI web_search tool): only proposes URLs, which are then
# fetched and checked like any other source. Used only when fields are missing.
LLM_WEB_SEARCH = True
MAX_WEB_SEARCHES_PER_RUN = 20
MAX_WEB_SEARCH_URLS = 5

# LLM extraction.
MAX_SOURCES_PER_LLM_CALL = 8
MAX_SOURCE_CHARS = 12000
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

# Fields whose absence triggers the LLM web search.
KEY_PERSON_FIELDS = ("age", "residence", "fault")
KEY_INCIDENT_FIELDS = ("date", "location")

REQUEST_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
