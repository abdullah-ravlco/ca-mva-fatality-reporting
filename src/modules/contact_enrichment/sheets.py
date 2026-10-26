"""Send contacts.csv rows to a Google Sheet through a small Google Apps Script web app that you deploy
from the sheet (apps_script.gs). The script appends only the people whose Incident ID is not in the tab yet.

Set in .env (the first two are needed; without them nothing here runs and the CSV/JSON files are the output):
  GOOGLE_SCRIPT_URL    the Web app URL from Deploy > New deployment (ends in /exec)
  GOOGLE_SCRIPT_TOKEN  a long random string; the same value as the script property TOKEN in the script
  GOOGLE_SHEET_TAB     optional: the name of an existing tab that receives the rows (default: the first tab).
                       The script never creates a tab; a name that does not exist is an error.

The URL plus the token are the only protection: anyone who has both can write to the sheet. Keep them in .env
only. The tab holds personal data; share the spreadsheet only with the people who work these leads.
"""

import logging

import requests

from src.modules.contact_enrichment import config

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 90


def enabled():
    return bool(config.GOOGLE_SCRIPT_URL and config.GOOGLE_SCRIPT_TOKEN)


def table(columns, rows):
    """One list per row, in column order; empty cells as ''."""
    return [["" if row.get(c) is None else str(row[c]) for c in columns] for row in rows]


def publish(tab, columns, rows):
    """Append the rows that are new to the tab. Returns the script's answer, e.g. {"status": "success",
    "added": 3, "skipped": 6}. Raises when the request fails or the script answers with an error."""
    response = requests.post(
        config.GOOGLE_SCRIPT_URL,
        json={"token": config.GOOGLE_SCRIPT_TOKEN, "tab": tab, "columns": list(columns), "rows": table(columns, rows)},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    try:
        answer = response.json()
    except ValueError:
        raise RuntimeError("the script did not answer with JSON: the deployment may need a Google sign-in "
                           "(set Who has access to Anyone) or the URL does not end in /exec: " + response.text[:120])
    if answer.get("status") != "success":
        raise RuntimeError(f"the script refused the request: {answer.get('message')}")
    return answer
