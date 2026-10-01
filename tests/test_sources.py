import httpx
import respx

from jobsearcher.sources import jobtech_links, platsbanken
from jobsearcher.sources.jobtech_links import JobTechLinksSource
from jobsearcher.sources.platsbanken import PlatsbankenSource


def test_platsbanken_parse_hit(load_fixture):
    hit = load_fixture("platsbanken_search.json")["hits"][0]
    job = platsbanken.parse_hit(hit)
    assert job.title.startswith("Python-utvecklare")
    assert job.company == "Exempel AB"
    assert job.company_org_nr == "5561234567"
    assert job.location == "Stockholm"
    assert job.apply_url == "https://exempel.teamtailor.com/jobs/123"
    assert job.remote is True
    named = [c for c in job.contacts if c.name]
    assert named[0].name == "Anna Svensson"
    assert named[0].provenance == "platsbanken:application_contacts"


def test_platsbanken_skips_removed_and_incomplete():
    assert platsbanken.parse_hit({"id": "1", "headline": "x", "removed": True}) is None
    assert platsbanken.parse_hit({"id": "1"}) is None


@respx.mock
def test_platsbanken_search_paginates(load_fixture, monkeypatch):
    monkeypatch.setattr(platsbanken, "PAGE_SIZE", 1)
    data = load_fixture("platsbanken_search.json")
    pages = [{**data, "hits": [h]} for h in data["hits"]]
    route = respx.get("https://jobsearch.api.jobtechdev.se/search").mock(
        side_effect=[httpx.Response(200, json=p) for p in pages]
    )
    jobs = list(PlatsbankenSource(client=httpx.Client()).search("python"))
    assert [j.title for j in jobs] == [h["headline"] for h in data["hits"]]
    assert route.call_count == 2
    assert route.calls[1].request.url.params["offset"] == "1"


@respx.mock
def test_retries_on_server_error(load_fixture, monkeypatch):
    monkeypatch.setattr("jobsearcher.sources.base.time.sleep", lambda s: None)
    respx.get("https://jobsearch.api.jobtechdev.se/search").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=load_fixture("platsbanken_search.json")),
        ]
    )
    assert len(list(PlatsbankenSource(client=httpx.Client()).search("python"))) == 2


def test_links_parse_hit(load_fixture):
    job = jobtech_links.parse_hit(load_fixture("jobtech_links_search.json")["hits"][0])
    assert job.url == "https://exempel.se/jobb/123"
    assert job.location == "Stockholm"
    assert job.sources[0].source == "jobtech_links"


@respx.mock
def test_links_search(load_fixture):
    respx.get("https://links.api.jobtechdev.se/joblinks").mock(
        return_value=httpx.Response(200, json=load_fixture("jobtech_links_search.json"))
    )
    assert len(list(JobTechLinksSource(client=httpx.Client()).search("python"))) == 1


def _links_hit(*urls: str) -> dict:
    return {
        "id": "x",
        "headline": "Projektledare",
        "source_links": [{"label": "l", "url": u} for u in urls],
    }


def test_links_only_to_platsbanken():
    af = "https://arbetsformedlingen.se/platsbanken/annonser/{}"
    assert jobtech_links.links_only_to_platsbanken(_links_hit(af.format(1), af.format(2)))
    assert not jobtech_links.links_only_to_platsbanken(
        _links_hit(af.format(1), "https://ledigajobb.se/jobb/1")
    )
    assert not jobtech_links.links_only_to_platsbanken(_links_hit())


@respx.mock
def test_links_search_skips_platsbanken_only(load_fixture):
    data = load_fixture("jobtech_links_search.json")
    af_hit = _links_hit("https://arbetsformedlingen.se/platsbanken/annonser/31525072")
    respx.get("https://links.api.jobtechdev.se/joblinks").mock(
        return_value=httpx.Response(200, json={**data, "hits": [*data["hits"], af_hit]})
    )
    skipping = JobTechLinksSource(client=httpx.Client(), skip_platsbanken_only=True)
    assert [j.url for j in skipping.search("python")] == ["https://exempel.se/jobb/123"]
    keeping = JobTechLinksSource(client=httpx.Client())
    assert len(list(keeping.search("python"))) == 2
