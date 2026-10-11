from jobsearcher.config import Config, JobSpyConfig, SearchConfig
from jobsearcher.models import Job
from jobsearcher.pipeline import matches_filters
from jobsearcher.places import match_terms, resolve
from jobsearcher.regions import SearchArea, Target, profile_areas, search_plan
from jobsearcher.sources import enabled_sources, has_slow_sources, slow_sources
from jobsearcher.sources.jobspy_source import JobSpySource


def test_places_resolve_to_countries_in_any_language():
    def where(entry):
        place = resolve(entry)
        return place and (place.name, place.country, place.kind)

    assert where("München") == ("Munich", "DE", "city")
    assert where("Rom") == ("Rome", "IT", "city")
    assert where("Göteborg") == ("Göteborg", "SE", "city")  # Swedish places as written
    assert where("Dublin") == ("Dublin", "IE", "city")  # the biggest Dublin
    assert where("Tyskland") == ("Germany", "DE", "country")
    assert where("UK") == ("United Kingdom", "GB", "country")
    assert where("Scotland") == ("Scotland", "GB", "area")
    assert where("Cork, Irland") == ("Cork", "IE", "city")
    assert where("Remote") is None and where("Narnia") is None


def test_the_filter_knows_a_place_by_its_other_names():
    def job(location, region=None):
        return Job(id="x", title="Project Manager", location=location, region=region)

    munich = SearchConfig(locations=["München"], include_remote=False)
    assert matches_filters(job("Munich", "Bavaria, Germany"), munich)
    assert matches_filters(job("München"), munich)
    assert matches_filters(job("Zurich"), SearchConfig(locations=["Zürich"]))  # accents aside
    germany = SearchConfig(locations=["Tyskland"])
    assert matches_filters(job("Lübbecke", "Germany"), germany)
    assert not matches_filters(job("Wien", "Austria"), germany)
    assert match_terms("Bern, Switzerland") == ("Bern",)  # the city, not all of the country


def test_cities_are_searched_one_by_one_until_there_are_many():
    areas, unknown = profile_areas(
        ["Stockholm", "Göteborg", "Gothenburg", "Malmö", "Lund", "Uppsala",
         "Berlin", "München", "Dublin", "Ireland", "Narnia", "Remote"]
    )  # fmt: skip
    assert [a.label() for a in areas] == [
        "Sweden",  # more than three of its cities: the whole country
        "Berlin, Germany",
        "Munich, Germany",
        "Ireland",  # named itself: the whole country
    ]
    assert unknown == ["Narnia"]
    assert profile_areas([]) == ([SearchArea(country="SE")], [])  # no region: all of Sweden


def _profile(locations, keywords):
    return Config(
        search=SearchConfig(locations=locations, keywords=keywords, use_target_roles=False)
    )


def test_the_plan_gives_the_first_country_every_keyword_and_merges_profiles():
    plan = search_plan(
        [
            _profile(["Stockholm", "Dublin"], ["projektledare", "Project Manager"]),
            _profile(["Dublin", "Lund"], ["Data Engineer"]),
            _profile(["Berlin"], []),  # nothing to search for yet: adds nothing
        ]
    )
    targets = {t.area.label(): (t.primary, t.keywords) for t in plan.targets}
    assert targets == {
        "Stockholm, Sweden": (True, ["projektledare", "Project Manager"]),
        "Dublin, Ireland": (True, ["projektledare", "Project Manager", "Data Engineer"]),
        "Lund, Sweden": (False, ["Data Engineer"]),
    }
    assert plan.countries == {"SE", "IE"}
    assert plan.by_country()["SE"] == ["projektledare", "Project Manager", "Data Engineer"]


def test_sources_search_the_countries_they_cover(monkeypatch):
    import jobsearcher.sources.jobspy_source as module

    monkeypatch.setattr(module, "available", lambda: True)
    config = Config()
    config.sources.environmentjob = True
    config.sources.eurojobs.enabled = True
    config.sources.jobspy = JobSpyConfig(sites=["linkedin"])
    names = lambda sources: [s.name for s in sources]  # noqa: E731

    sweden = search_plan([_profile(["Uppsala", "Dublin"], ["HR"])])
    assert names(enabled_sources(config, sweden)) == ["platsbanken", "jobtech_links", "linkedin"]
    assert names(slow_sources(config, sweden)) == ["eurojobs", "linkedin"]  # Dublin's later

    uk = search_plan([_profile(["Scotland"], ["Ecologist"])])
    assert names(enabled_sources(config, uk)) == ["environmentjob", "linkedin"]
    assert names(slow_sources(config, uk)) == ["eurojobs"]
    config.sources.eurojobs.enabled = False
    assert not has_slow_sources(config, uk) and has_slow_sources(config, sweden)

    config.sources.arbeitsagentur.enabled = True  # Germany's board: only with a German place
    assert "arbeitsagentur" not in names(enabled_sources(config, sweden))
    berlin = search_plan([_profile(["Uppsala", "Berlin"], ["HR"])])
    assert "arbeitsagentur" in names(enabled_sources(config, berlin))

    config.sources.eures.enabled = True  # after ranking; Sweden is left to Platsbanken
    [eures] = [s for s in slow_sources(config, sweden) if s.name == "eures"]
    assert list(eures.countries) == ["IE"]


def test_jobspy_searches_each_area_that_wants_the_keyword(monkeypatch):
    import jobsearcher.sources.jobspy_source as module

    # Indeed has a site in Ireland and Germany, not in Iceland.
    monkeypatch.setattr(module, "indeed_country", {"IE": "ireland", "DE": "germany"}.get)
    calls, sleeps = [], []

    def scrape(**kwargs):
        calls.append(kwargs)
        return []

    targets = [
        Target(area=SearchArea(country="IE", place="Dublin"), keywords=["HRBP"], primary=False),
        Target(area=SearchArea(country="DE"), keywords=["HRBP", "COO"], primary=False),
        Target(area=SearchArea(country="IS"), keywords=["HRBP"], primary=False),
    ]
    settings = JobSpyConfig(sites=["linkedin", "indeed"])
    linkedin = JobSpySource("linkedin", settings, targets, 30, scrape, sleeps.append)
    list(linkedin.search("hrbp"))
    assert [c["location"] for c in calls] == ["Dublin, Ireland", "Germany", "Iceland"]
    assert sleeps == [30, 30]
    calls.clear()
    indeed = JobSpySource("indeed", settings, targets, 30, scrape, sleeps.append)
    list(indeed.search("COO"))
    assert [(c["location"], c["country_indeed"]) for c in calls] == [("Germany", "germany")]
