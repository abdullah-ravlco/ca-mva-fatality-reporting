from src.modules.source_collection.case_grouping import group_with_reasons


def article(url, title, date, place="Fresno", road="Highway 99"):
    return {"url": url, "title": title, "incident_date": date, "place": place, "road": road}


def test_different_victims_of_one_crash_join_one_incident():
    driver = article("https://a.example/1", "Driver, 25, killed in crash on Highway 99 in Fresno", "2026-10-10T07:00:00+00:00")
    passenger = article("https://b.example/2", "Passenger, 31, dies after Highway 99 collision in Fresno", "2026-10-10T07:00:00+00:00")

    groups = group_with_reasons([driver, passenger])

    assert len(groups) == 1
    members, rationale = groups[0]
    assert len(members) == 2
    assert rationale == [{
        "joined": "https://b.example/2",
        "matched": "https://a.example/1",
        "reason": "same place/road and crash date",
    }]


def test_same_road_three_days_apart_stays_separate():
    first = article("https://a.example/1", "Driver killed in crash on Highway 99 in Fresno", "2026-10-07T07:00:00+00:00")
    second = article("https://b.example/2", "Driver killed in crash on Highway 99 in Fresno", "2026-10-10T07:00:00+00:00")

    assert len(group_with_reasons([first, second])) == 2


def test_two_crashes_in_one_city_on_one_day_stay_separate_when_texts_differ():
    pedestrian = dict(
        article("https://a.example/1", "Pedestrian killed in Hollywood crosswalk", "2026-10-10T07:00:00+00:00",
                place="Los Angeles", road=None),
        text=" ".join(
            "A pedestrian walking across Sunset Boulevard at Highland Avenue in Hollywood was struck "
            "by a white sedan late Friday and died at a hospital. Witnesses told officers the sedan "
            "entered the crosswalk on a green arrow. Detectives from West Traffic Division are reviewing "
            "surveillance footage from nearby storefronts and asked anyone with dashcam video to call. "
            "The woman, whose name was withheld pending notification of relatives, lived nearby and "
            "regularly crossed that intersection walking home from her evening shift downtown".split()
        ),
    )
    motorcyclist = dict(
        article("https://b.example/2", "Motorcyclist killed in South LA hit-and-run", "2026-10-10T07:00:00+00:00",
                place="Los Angeles", road=None),
        text=" ".join(
            "A motorcyclist riding north on Avalon Boulevard near Slauson Avenue in South Los Angeles "
            "collided with a pickup truck whose driver fled eastbound without stopping. Paramedics "
            "pronounced the rider dead at the scene. Investigators recovered a broken mirror and "
            "headlight fragments and are searching for a dark gray Ford pickup with front damage. "
            "Relatives gathered near the scene Saturday morning, placing candles beside the bent "
            "motorcycle frame while neighbors described repeated complaints about speeding there".split()
        ),
    )

    assert len(group_with_reasons([pedestrian, motorcyclist])) == 2


def test_different_cities_stay_separate():
    first = article("https://a.example/1", "Pedestrian killed in Fresno crash", "2026-10-10T07:00:00+00:00", road=None)
    second = article("https://b.example/2", "Pedestrian killed in Oakland crash", "2026-10-10T07:00:00+00:00",
                     place="Oakland", road=None)

    assert len(group_with_reasons([first, second])) == 2
