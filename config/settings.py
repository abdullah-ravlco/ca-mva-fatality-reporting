"""Central settings loader: environment variables and shared defaults.

Module-specific configuration (API thresholds, regexes, per-module paths)
stays in each module (e.g. src/modules/lead_verification/config.py or
src/modules/source_collection/sources.py); this file only holds what is
shared across modules: env vars, top-level paths, and generic HTTP/timezone
defaults used by src/core.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"

# How far back the first run collects, when there is no earlier run and no --since.
FIRST_RUN_LOOKBACK_DAYS = 7

# Oldest crash a news story may describe and still be kept. The window only decides which stories
# are fetched (by publication date); a story published in it about an older crash is kept, and
# incident grouping matches it to an existing incident or makes a new one.
MAX_CRASH_AGE_DAYS = 730


def module_data_dir(module_name):
    """data/<module_name>."""
    return DATA_DIR / module_name


# --------------------------------------------------
# API keys / env vars
# --------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

# --------------------------------------------------
# Shared HTTP / timezone defaults (used by src/core)
# --------------------------------------------------

TIMEZONE = "America/Los_Angeles"
PUBLISHER_TIMEZONE = TIMEZONE

USER_AGENT = "CaliforniaCrashSourceCollector/1.1"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
REQUEST_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 25
BROWSER_RETRY_ON_403 = True
