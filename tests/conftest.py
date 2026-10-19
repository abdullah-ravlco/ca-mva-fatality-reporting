import os

# Tests assume a 7-day window ending now, whatever the local .env sets
# (a real environment variable wins over .env).
os.environ["LOOKBACK_HOURS"] = "168"
