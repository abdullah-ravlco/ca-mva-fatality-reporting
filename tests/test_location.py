import pytest

from src.modules.source_collection.location import (
    CALIFORNIA_CONFIRMED,
    LOCATION_UNCONFIRMED,
    OUTSIDE_CALIFORNIA,
    detect_location,
)


def status(text):
    return detect_location(text)["location_status"]


def test_california_only_interstate():
    assert status("Crash on I-805 in National City") == CALIFORNIA_CONFIRMED


def test_california_county():
    result = detect_location("Fatal crash in Riverside County")
    assert result["location_status"] == CALIFORNIA_CONFIRMED
    assert result["county"] == "Riverside"


def test_california_city():
    assert status("Man killed in Bakersfield crash") == CALIFORNIA_CONFIRMED


def test_other_state():
    assert status("Driver killed in crash in Arizona") == OUTSIDE_CALIFORNIA


def test_no_location():
    assert status("Driver killed in crash on Main Street") == LOCATION_UNCONFIRMED


# ------------------------------------------------ residence is not the crash location

@pytest.mark.parametrize("title", [
    "Semi driver from California dies in Wednesday crash on I-35 in Olathe",
    "California man killed in NB I-35 tractor-trailer crash that took hours to clear",
    "84-year-old California woman identified in fatal Honolulu parking garage crash",
    "Liberal man killed in crash between semis in western Kansas",
    "N.J. motorcyclist killed in collision with pickup truck",
    "Driver, motorcyclist killed in wrong-way I-94 crash south of St. Cloud",
])
def test_out_of_state_crash_is_not_confirmed_by_a_california_resident(title):
    assert detect_location(title)["location_status"] == OUTSIDE_CALIFORNIA


@pytest.mark.parametrize("title", [
    "Fatal crash shuts down SB U.S. 101 near Gilroy, CHP says",
    "Nevada man killed in crash on I-15 near Barstow, CHP says",
    "Southern California woman killed in single-car crash, CHP says",
    "Motorcyclist killed in crash in Nevada County, CHP says",
])
def test_california_crash_stays_confirmed(title):
    assert detect_location(title)["location_status"] == CALIFORNIA_CONFIRMED
