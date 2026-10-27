"""Live check of the Google Sheet export: talks to your deployed Apps Script web app.

Skipped unless GOOGLE_SCRIPT_URL and GOOGLE_SCRIPT_TOKEN are set in .env. It writes only fake people
(last name "Test-...") to a tab named PipelineTest. The script never creates a tab, so add an empty tab
with that name to the spreadsheet first, and delete it afterwards.

  .venv/bin/python -m pytest tests/test_google_sheet_live.py -v -s
"""

import uuid

import pytest
import requests

from src.modules.contact_enrichment import config, sheets

pytestmark = pytest.mark.skipif(
    not sheets.enabled(), reason="set GOOGLE_SCRIPT_URL and GOOGLE_SCRIPT_TOKEN in .env to run the live Google Sheet test"
)

TAB = "PipelineTest"
COLUMNS = ["First Name", "Last Name", "Crash Date", "Phone1"]


def fake_people():
    run = uuid.uuid4().hex[:8]  # new ids every run, so the test can be repeated
    return [
        {"First Name": "Test", "Last Name": f"One-{run}", "Crash Date": "2026-01-01", "Phone1": "5595550101 (Mobile)"},
        {"First Name": "Test", "Last Name": f"Two-{run}", "Crash Date": "2026-01-01", "Phone1": "5595550102 (Mobile)"},
    ]


def test_the_web_app_is_reachable_and_rejects_a_wrong_token():
    response = requests.post(
        config.GOOGLE_SCRIPT_URL,
        json={"token": "not-the-token", "tab": TAB, "columns": COLUMNS, "rows": []},
        timeout=sheets.TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    try:
        answer = response.json()
    except ValueError:
        pytest.fail("The URL answered with a web page, not JSON: the deployment probably needs a Google sign-in. "
                    "Redeploy with Who has access: Anyone, and use the URL that ends in /exec. Got: " + response.text[:150])
    assert answer == {"status": "error", "message": "bad token"}


def test_new_people_are_added_and_a_repeat_adds_nobody():
    people = fake_people()

    first = sheets.publish(TAB, COLUMNS, people)
    assert first["status"] == "success" and first["added"] == 2 and first["skipped"] == 0

    again = sheets.publish(TAB, COLUMNS, people)
    assert again["added"] == 0 and again["skipped"] == 2


def test_only_the_new_person_is_added_when_one_is_already_there():
    people = fake_people()
    sheets.publish(TAB, COLUMNS, people[:1])

    answer = sheets.publish(TAB, COLUMNS, people)
    assert answer["added"] == 1 and answer["skipped"] == 1
