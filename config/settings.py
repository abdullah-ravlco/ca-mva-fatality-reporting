"""Central settings loader: environment variables and shared defaults.

Module-specific configuration (API thresholds, regexes, per-module paths)
stays in each module (e.g. src/modules/lead_verification/config.py or
src/modules/source_collection/sources.py); this file only holds what is
shared across modules: env vars, top-level paths, and generic HTTP/timezone
defaults used by src/core.
"""

import math
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"


def module_data_dir(module_name):
    """data/<module_name>."""
    return DATA_DIR / module_name


# --------------------------------------------------
# API keys / env vars
# --------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

# --------------------------------------------------
# Collection window
# --------------------------------------------------

# Normally the last LOOKBACK_HOURS hours (168 = 7 days) up to now. To collect a fixed period
# instead, set COLLECT_FROM and COLLECT_TO (YYYY-MM-DD, both days included) in .env, e.g.
# 2026-09-01 and 2026-09-30. Everything is then judged as of the end of that period.
def window_for(collect_from, collect_to, timezone_name):
    """(window_end, lookback_hours) for a fixed period; window_end is midnight after collect_to."""
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(collect_from, time.min, tzinfo=zone)
    end = datetime.combine(collect_to + timedelta(days=1), time.min, tzinfo=zone)
    return end, math.ceil((end - start).total_seconds() / 3600)


def _env_date(name):
    value = (os.getenv(name) or "").strip()
    return date.fromisoformat(value) if value else None


COLLECT_FROM = _env_date("COLLECT_FROM")
COLLECT_TO = _env_date("COLLECT_TO")
if bool(COLLECT_FROM) != bool(COLLECT_TO):
    raise ValueError("Set both COLLECT_FROM and COLLECT_TO in .env, or neither.")
if COLLECT_FROM and COLLECT_TO:
    WINDOW_END, LOOKBACK_HOURS = window_for(COLLECT_FROM, COLLECT_TO, "America/Los_Angeles")
else:
    WINDOW_END = None
    LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS") or 168)

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
