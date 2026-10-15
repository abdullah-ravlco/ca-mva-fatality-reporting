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


# --------------------------------------------------------------- headline-only leads

def headline(url, title, published="2026-10-13T12:00:00+00:00", **extra):
    return dict({"url": url, "title": title, "published_at": published, "incident_date": None}, **extra)


def test_same_crash_with_different_wording_merges_on_distinctive_words():
    first = headline("https://a.example/1", "Motorcyclist Killed in Lompoc Crash With Car on East Ocean Avenue")
    second = headline("https://b.example/2", "Lompoc Motorcyclist Dies in Collision With Vehicle on East Ocean Avenue")

    members, rationale = group_with_reasons([first, second])[0]

    assert len(members) == 2
    assert rationale[0]["reason"] == "overlapping distinctive headline words"


def test_shared_victim_name_merges_coroner_entry_and_news():
    coroner = {
        "url": "https://sheriff.example/7410", "title": "Riverside County Coroner file 2026-07531",
        "source_type": "coroner", "incident_date": "2026-10-10T07:00:00+00:00",
        "description": "Coroner's File #: 2026-07531 | Last Name: Monterola-Alas | First Name: Dylan | Age: 26 Years |",
    }
    news = headline("https://b.example/2", "Riverside, CA – Motorcyclist Dylan Monterola-Alas Dies in Crash on Martin Luther King Boulevard",
                    incident_date="2026-10-10T07:00:00+00:00")

    groups = group_with_reasons([coroner, news])

    assert len(groups) == 1
    assert groups[0][1][0]["reason"] == "same victim name"


def test_generic_words_alone_do_not_merge_different_crashes():
    first = headline("https://a.example/1", "Motorcyclist killed in crash on curve near Waterford")
    second = headline("https://b.example/2", "Motorcyclist killed after crashing into tree in Redding")

    assert len(group_with_reasons([first, second])) == 2


def test_same_city_different_crash_stays_separate_without_shared_details():
    first = headline("https://a.example/1", "Coroner identifies four killed in fiery Highway 58 crash in Bakersfield",
                     place="Bakersfield", road="Highway 58")
    second = headline("https://b.example/2", "Pedestrian killed in crash on Rosedale Highway in Bakersfield", place="Bakersfield")

    assert len(group_with_reasons([first, second])) == 2


def test_stories_published_far_apart_without_crash_dates_stay_separate():
    first = headline("https://a.example/1", "Motorcyclist Killed in Lompoc Crash on East Ocean Avenue", published="2026-10-07T12:00:00+00:00")
    second = headline("https://b.example/2", "Lompoc Motorcyclist Dies on East Ocean Avenue", published="2026-10-13T12:00:00+00:00")

    assert len(group_with_reasons([first, second])) == 2


def test_cases_bridged_by_a_later_record_are_merged():
    # a and c do not match each other; b matches both. The first pass leaves c in its
    # own case when it arrives before b, and the second pass must join them.
    a = headline("https://a.example/1", "Elio Battisti killed in Adelanto pedestrian crash")
    b = headline("https://b.example/2", "Elio Battisti killed on Air Expressway in Adelanto pedestrian crash")
    c = headline("https://c.example/3", "Air Expressway pedestrian crash: man killed")

    assert len(group_with_reasons([a, c])) == 2
    for order in ([a, b, c], [a, c, b], [c, a, b], [c, b, a]):
        assert len(group_with_reasons(order)) == 1


def test_case_id_is_shared_by_duplicates_and_independent_of_record_order():
    from src.modules.source_collection.case_grouping import case_entry

    first = headline("https://a.example/1", "Motorcyclist Killed in Lompoc Crash With Car on East Ocean Avenue")
    second = headline("https://b.example/2", "Lompoc Motorcyclist Dies in Collision With Vehicle on East Ocean Avenue")

    ids = {
        case_entry(*group_with_reasons(order)[0])["case_id"]
        for order in ([first, second], [second, first])
    }
    entry = case_entry(*group_with_reasons([first, second])[0])

    assert len(ids) == 1
    assert [s["url"] for s in entry["sources"]] == ["https://a.example/1", "https://b.example/2"]


def test_headlines_made_of_the_same_words_merge():
    first = headline("https://a.example/1", "2 Chico residents identified as victims of fatal Sutter County crash",
                     place="Chico", county="Sutter")
    second = headline("https://b.example/2", "Chico residents identified in deadly head-on Highway 99 crash in Sutter County",
                      place="Chico", county="Sutter")

    assert len(group_with_reasons([first, second])) == 1


def test_same_place_and_victim_age_merge_but_different_ages_do_not_by_age_alone():
    first = headline("https://a.example/1", "14-year-old boy dies riding electric motorcycle in Turlock", place="Turlock")
    second = headline("https://b.example/2", "Neighbors in Turlock mourn death of 14-year-old", place="Turlock")

    members, rationale = group_with_reasons([first, second])[0]

    assert len(members) == 2
    assert rationale[0]["reason"] == "same place and victim age"
