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
    assert job.occupation_field == "Data/IT"
    assert job.occupation_group == "Mjukvaru- och systemutvecklare m.fl."
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
    assert job.occupation_field == "Data/IT"


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


def test_environmentjob_reads_the_sitemap_and_job_pages():
    import json

    from test_companies import FakeClient

    from jobsearcher.sources.environmentjob import SITEMAP, EnvironmentJobSource

    def page(title, region, text):
        posting = {
            "@context": "https://schema.org", "@type": "JobPosting", "title": title,
            "description": f"<p>{text}</p>", "datePosted": "2026-10-01T13:26:27Z",
            "validThrough": "2026-10-18T22:55:00Z", "employmentType": "FULL_TIME",
            "hiringOrganization": {"@type": "Organization", "name": "Example Wildlife Trust"},
            "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress",
                "addressCountry": "United Kingdom", "addressLocality": "Nottingham",
                "addressRegion": region}},
        }  # fmt: skip
        return f'<script type="application/ld+json">{json.dumps(posting)}</script>'

    base = "https://www.environmentjob.co.uk/jobs/"
    client = FakeClient(
        {
            SITEMAP: f"<urlset><url><loc>{base}1-strategic-ecologist</loc></url>"
            f"<url><loc>{base}2-ranger</loc></url><url><loc>{base}3-gone</loc></url>"
            "<url><loc>https://www.environmentjob.co.uk/jobs/49-ecology-sector</loc></url></urlset>",
            f"{base}1-strategic-ecologist": page(
                "Strategic Ecologist", "Nottinghamshire", "Lead our project management of surveys."
            ),  # fmt: skip
            f"{base}2-ranger": page("Seasonal Ranger", "Highlands", "Guide walks."),
            "https://www.environmentjob.co.uk/jobs/49-ecology-sector": page("x", "y", "z"),
        }
    )
    source = EnvironmentJobSource(client)
    [job] = list(source.search("project management"))
    assert job.title == "Strategic Ecologist" and job.company == "Example Wildlife Trust"
    assert job.location == "Nottingham" and job.region == "Nottinghamshire, United Kingdom"
    assert job.deadline is not None and job.sources[0].source == "environmentjob"
    assert [j.title for j in source.search("ranger")] == ["Seasonal Ranger"]
    assert list(source.search("manage")) == []  # whole words only
    fetched = len(client.requests)
    list(source.search("ecologist"))
    assert len(client.requests) == fetched  # the board is read once per run


def test_eurojobs_searches_countries_and_reads_job_pages():
    from test_companies import FakeClient

    from jobsearcher.config import EurojobsConfig
    from jobsearcher.sources.eurojobs import BASE, EurojobsSource

    def card(job_id, title, snippet, posted):
        return f"""<article class="ej-job-result ej-job-result--basic">
            <a href="{BASE}/jobs/{job_id}/x-{job_id}" class="ej-job-result__title">{title}</a>
            <span><strong>Location:</strong> Somewhere</span>
            <span><strong>Posted:</strong> {posted}</span><p>{snippet}</p></article>"""

    def job_page(title, location, employer):
        def field(label, value):
            return (f'<div class="small text-muted">{label}</div>'
                    f'<div class="fw-semibold text-dark">{value}</div>')  # fmt: skip

        return (
            f'<h1 class="ej-job-detail-title display-6">{title}</h1><h2>Job information</h2>'
            + field("Location", location) + field("Company", "Agency AB")
            + field("Client / Employer", employer) + field("Employment type", "full_time")
            + field("Posted", "10.10.2026") + field("Closing date", "25.11.2026")
            + '<section><h2>Job description</h2><div class="ej-job-detail-section-content">'
            "Lead the project. Contact: "
            '<a href="/cdn-cgi/l/email-protection" class="__cf_email__" '
            # "anna@ex.seRecruiter", XORed with 0x42 as Cloudflare does
            + 'data-cfemail="42' + bytes(b ^ 0x42 for b in b"anna@ex.seRecruiter").hex()
            + '">[email&#160;protected]</a></div></section>'
            + field("Company", "Company info, not the ad")
        )  # fmt: skip

    from datetime import datetime

    today = datetime.now().strftime("%d.%m.%Y")
    next_url = f"{BASE}/jobs?q=project+manager&country=198&page=2"
    client = FakeClient(
        {
            f"{BASE}/browse/countries": f'<a href="{BASE}/jobs?country=198" '
            'class="ej-browse-page-card"><img src="/assets/flags/se.svg"> Sweden 9,924 Jobs</a>'
            f'<a href="{BASE}/jobs?country=56" '
            'class="ej-browse-page-card"><img src="/assets/flags/de.svg"> Germany</a>',
            # Page 1 (the client ignores the query): a match, a non-match, then page 2.
            f"{BASE}/jobs": card(1, "Project Manager", "Lead projects.", today)
            + card(2, "Chef", "Manage a kitchen project.", today)
            + f'<a href="{next_url.replace("&", "&amp;")}" rel="next">Next</a>',
            next_url: card(1, "Project Manager", "Lead projects.", today)  # seen already
            + card(3, "Senior project manager", "", today)
            + card(4, "Project manager", "", "01.01.2020"),  # too old: not opened
            f"{BASE}/jobs/1/x-1": job_page("Project Manager", "Gothenburg, Sweden", "Volvo"),
            f"{BASE}/jobs/3/x-3": job_page("Senior project manager", "Remote", "Acme"),
        }
    )
    sleeps = []
    config = EurojobsConfig(enabled=True)
    # Germany only wants another keyword; eurojobs has no jobs in Narnia ("NA").
    countries = {"SE": ["Project Manager"], "DE": ["HR Generalist"], "NA": ["Project Manager"]}
    source = EurojobsSource(client, config, countries, sleep=sleeps.append, clock=lambda: 100.0)
    first, second = list(source.search("project manager"))
    assert sleeps == [config.search_interval_s]  # between the two search pages
    assert first.title == "Project Manager" and first.company == "Volvo"
    assert first.location == "Göteborg" and first.region is None  # the filter finds the county
    assert first.employment_type == "full time" and first.deadline.day == 25
    assert [c.email for c in first.contacts] == ["anna@ex.se"]
    assert second.remote and second.location is None
    assert f"{BASE}/jobs/2/x-2" not in client.requests  # the keyword isn't in its card
    assert f"{BASE}/jobs/4/x-4" not in client.requests
