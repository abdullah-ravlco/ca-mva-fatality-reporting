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
