import os

# Tests assume the default 7-day window ending now, whatever the local .env sets
# (a real environment variable wins over .env).
os.environ["LOOKBACK_HOURS"] = "168"
os.environ["COLLECT_FROM"] = ""
os.environ["COLLECT_TO"] = ""
