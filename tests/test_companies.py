from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobsearcher.companies.config import AtsRef, Company, load_companies, slugify
from jobsearcher.companies.crawl import CompanyCrawlReport, crawl_feeds, resolve_feeds
from jobsearcher.companies.detect import careers_links, detect, find_ats
from jobsearcher.companies.http import PoliteClient
from jobsearcher.config import CompaniesSettings, Config, SearchConfig
from jobsearcher.models import Job, JobStatus, SourceRef, make_job_id
from jobsearcher.pipeline import run_search
from jobsearcher.sources.ats import greenhouse, lever, smartrecruiters, teamtailor, varbi
from jobsearcher.sources.ats.common import html_to_text, is_sweden, swedish_city
from jobsearcher.store import Store

ACME = Company(name="Acme Sverige AB", website="https://acme.se", location="Stockholm")


class FakeClient:
    """Serves canned responses by URL (ignoring query strings) and records requests."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def _get(self, url, params=None):
        self.requests.append(url)
        if url not in self.routes:
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", url), response=httpx.Response(404)
            )
        return self.routes[url]

    def get_json(self, url, params=None):
        return self._get(url, params)

    def get_text(self, url, params=None):
        return self._get(url, params)

    def get(self, url, params=None):
        body = self._get(url, params)
        return httpx.Response(200, text=body, request=httpx.Request("GET", url))


def keep_all(job):
    return True


# --- helpers ---------------------------------------------------------------------


def test_html_to_text_keeps_structure():
    text = html_to_text(
        "<h2>Om rollen</h2><p>Du leder&nbsp;team.</p><ul><li>HR</li><li>Ops</li></ul>"
    )
    assert text == "Om rollen\n\nDu leder team.\n\n- HR\n- Ops"
    assert html_to_text("&lt;p&gt;escaped&lt;/p&gt;") == "escaped"
    assert html_to_text(None) == ""


def test_sweden_detection():
    assert is_sweden("SE") and is_sweden("Sweden") and is_sweden(None, "Stockholm, Sweden")
    assert is_sweden("GB") is False
    assert is_sweden(None, "Remote") is None
    assert swedish_city("Hybrid - Göteborg") == "Göteborg"
    assert swedish_city("London") is None
    assert swedish_city("Solna, Göteborg") == "Solna"  # the first named, not list order


def test_company_list(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(
        "companies:\n"
        "  - {name: Acme Sverige AB, website: https://acme.se}\n"
        "  - {name: Off AB, enabled: false}\n"
        "  - {name: H&M, ats: {type: smartrecruiters, ref: HMGroup}}\n"
    )
    companies = load_companies(path)
    assert [c.slug for c in companies] == ["acme-sverige-ab", "h-m"]
    assert companies[1].ats.ref == "HMGroup"
    assert companies[0].query == '"Acme Sverige AB"'
    assert slugify("Göteborgs Universitet") == "goteborgs-universitet"
    path.write_text("companies:\n  - {name: A}\n  - {name: a}\n")
    with pytest.raises(ValueError, match="Duplicate"):
        load_companies(path)


# --- adapters ----------------------------------------------------------------------


def _teamtailor_item(n, country="SE", city="Stockholm"):
    return {
        "id": f"uuid-{n}",
        "title": f"HR Business Partner {n}",
        "url": f"https://jobb.acme.se/jobs/{n}-hrbp",
        "date_published": "2026-09-20T09:00:00+02:00",
        "_jobposting": {
            "description": "<p>Du blir <strong>HRBP</strong>.</p>",
            "datePosted": "2026-09-20T09:00:00+02:00",
            "validThrough": "2026-10-31",
            "employmentType": "FULL_TIME",
            "jobLocation": [
                {"address": {"addressLocality": city, "addressCountry": country}},
            ],
        },
    }


def test_teamtailor_follows_pages_and_drops_jobs_abroad():
    client = FakeClient(
        {
            "https://jobb.acme.se/jobs.json": {
                "items": [_teamtailor_item(1), _teamtailor_item(2, "GB", "London")],
                "next_url": "https://jobb.acme.se/jobs.json?page=2",
            },
            "https://jobb.acme.se/jobs.json?page=2": {"items": [_teamtailor_item(3)]},
        }
    )
    jobs = list(teamtailor.fetch_jobs(client, "https://jobb.acme.se/", ACME, keep_all))
    assert [j.title for j in jobs] == ["HR Business Partner 1", "HR Business Partner 3"]
    job = jobs[0]
    assert job.location == "Stockholm" and job.company == "Acme Sverige AB"
    assert job.description == "Du blir HRBP."
    assert job.employment_type == "full time"
    assert job.sources == [
        SourceRef(source="teamtailor:jobb-acme-se", source_id="uuid-1", url=job.url)
    ]
    assert job.id == make_job_id("teamtailor:jobb-acme-se", "uuid-1")


VARBI_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>HR-strateg</title><link>https://acme.varbi.com/en/what:job/jobID:973127/</link>
<description>Arbetsuppgifter
Du driver HR-frågor.</description><pubDate>Thu, 01 Oct 2026 00:00:00 +0200</pubDate></item>
</channel></rss>"""


def test_varbi_uses_company_location():
    client = FakeClient({"https://acme.varbi.com/en/what:rssfeed/": VARBI_RSS})
    [job] = varbi.fetch_jobs(client, "acme", ACME, keep_all)
    assert (job.title, job.location) == ("HR-strateg", "Stockholm")
    assert job.sources[0].source_id == "973127"
    assert job.published_at.date().isoformat() == "2026-10-01"
    assert "Du driver HR-frågor." in job.description


def _lever(n, country, locations, workplace="hybrid"):
    return {
        "id": f"lever-{n}",
        "text": f"Operations Manager {n}",
        "hostedUrl": f"https://jobs.lever.co/acme/lever-{n}",
        "applyUrl": f"https://jobs.lever.co/acme/lever-{n}/apply",
        "country": country,
        "workplaceType": workplace,
        "createdAt": 1790000000000,
        "categories": {
            "location": locations[0],
            "allLocations": locations,
            "commitment": "Permanent",
        },
        "descriptionPlain": "Lead operations.",
        "lists": [{"text": "You have", "content": "<li>10 years</li>"}],
        "additionalPlain": "",
    }


def test_lever_keeps_swedish_offices():
    client = FakeClient(
        {
            "https://api.lever.co/v0/postings/acme": [
                _lever(1, "SE", ["Stockholm"]),
                _lever(2, "GB", ["London", "Stockholm"]),  # also in Stockholm
                _lever(3, "US", ["New York"], "remote"),
            ]
        }
    )
    jobs = list(lever.fetch_jobs(client, "acme", ACME, keep_all))
    assert [(j.title, j.location) for j in jobs] == [
        ("Operations Manager 1", "Stockholm"),
        ("Operations Manager 2", "Stockholm"),
    ]
    assert "You have\n- 10 years" in jobs[0].description
    assert jobs[0].apply_url.endswith("/apply")


def test_greenhouse_matches_swedish_locations():
    client = FakeClient(
        {
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs": {
                "jobs": [
                    {
                        "id": 1,
                        "title": "Change Manager",
                        "absolute_url": "https://acme.com/jobs?gh_jid=1",
                        "location": {"name": "Stockholm, Sweden"},
                        "content": "&lt;p&gt;Drive change.&lt;/p&gt;",
                        "first_published": "2026-09-01T10:00:00-04:00",
                    },
                    {
                        "id": 2,
                        "title": "Change Manager US",
                        "absolute_url": "https://acme.com/jobs?gh_jid=2",
                        "location": {"name": "Seattle"},
                    },
                ]
            }
        }
    )
    [job] = greenhouse.fetch_jobs(client, "acme", ACME, keep_all)
    assert (job.title, job.location, job.description) == (
        "Change Manager",
        "Stockholm",
        "Drive change.",
    )


def test_smartrecruiters_fetches_details_only_for_wanted_jobs():
    api = "https://api.smartrecruiters.com/v1/companies/Acme/postings"

    def posting(n, city):
        return {
            "id": str(n),
            "name": f"People Partner {n}",
            "location": {"city": city, "country": "se"},
        }

    client = FakeClient(
        {
            api: {"totalFound": 2, "content": [posting(1, "Stockholm"), posting(2, "Kiruna")]},
            f"{api}/1": {
                "postingUrl": "https://jobs.smartrecruiters.com/Acme/1-people-partner",
                "applyUrl": "https://jobs.smartrecruiters.com/Acme/1-people-partner?oga=true",
                "jobAd": {
                    "sections": {
                        "jobDescription": {"title": "Job Description", "text": "<p>Partner HR.</p>"}
                    }
                },
            },
        }
    )
    wanted = lambda job: job.location == "Stockholm"  # noqa: E731
    [job] = smartrecruiters.fetch_jobs(client, "Acme", ACME, wanted)
    assert job.description == "Job Description\nPartner HR."
    assert job.url == job.sources[0].url == "https://jobs.smartrecruiters.com/Acme/1-people-partner"
    assert f"{api}/2" not in client.requests  # Kiruna: no detail request


# --- detection ---------------------------------------------------------------------


def test_find_ats_patterns():
    assert find_ats('<a href="https://jobs.lever.co/acme/123">', "https://acme.se") == (
        "lever",
        "acme",
    )
    assert find_ats('src="https://boards.greenhouse.io/embed/job_board?for=acme"', "x") == (
        "greenhouse",
        "acme",
    )
    assert find_ats('href="https://acme.varbi.com/se/"', "x") == ("varbi", "acme")
    assert find_ats('href="https://acme.teamtailor.com/jobs"', "x") == (
        "teamtailor",
        "https://acme.teamtailor.com",
    )
    # Teamtailor's shared tracking host isn't a customer; the CDN means the page itself
    # is a Teamtailor site under the company's own domain.
    page = '<script src="https://tt.teamtailor.com/t.js"></script><img src="https://images.teamtailor-cdn.com/a.png">'
    assert find_ats(page, "https://jobba.acme.se/jobs") == ("teamtailor", "https://jobba.acme.se")
    assert find_ats("<p>nothing</p>", "x") is None


def test_careers_links_stay_on_site():
    html = """
      <a href="/om-oss">Om oss</a>
      <a href="/karriar">Karriär</a>
      <a href="https://jobba.acme.se/">Lediga jobb</a>
      <a href="https://www.linkedin.com/company/acme/jobs">LinkedIn</a>
      <a href="mailto:jobb@acme.se">Mejla</a>"""
    links = careers_links(html, "https://www.acme.se/")
    assert set(links) == {"https://www.acme.se/karriar", "https://jobba.acme.se/"}


def test_detect_follows_careers_link_and_validates_teamtailor():
    client = FakeClient(
        {
            "https://acme.se": '<a href="https://jobba.acme.se/">Jobba hos oss</a>',
            "https://jobba.acme.se/": '<link href="https://assets-aws.teamtailor-cdn.com/x.css">',
            "https://jobba.acme.se/jobs.json": {"items": []},
        }
    )
    result = detect(ACME, client)
    assert (result.ats_type, result.ats_ref, result.error) == (
        "teamtailor",
        "https://jobba.acme.se",
        None,
    )


def test_detect_reports_errors_and_respects_override():
    result = detect(ACME, FakeClient({}))
    assert result.ats_type is None and "404" in result.error
    override = ACME.model_copy(update={"ats": AtsRef(type="lever", ref="acme")})
    assert detect(override, FakeClient({})).ats_ref == "acme"


def test_polite_client_obeys_robots_and_rate_limit():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text="ok")

    sleeps, now = [], [0.0]
    client = PoliteClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        min_interval_s=1.0,
        sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)),
        clock=lambda: now[0],
    )
    assert client.get_text("https://acme.se/karriar") == "ok"
    with pytest.raises(httpx.HTTPError, match="robots.txt"):
        client.get("https://acme.se/private/x")
    assert sleeps == [1.0]  # robots.txt and the page were 0 s apart


# --- crawl + pipeline ----------------------------------------------------------------


def test_resolve_feeds_caches_detection():
    store = Store(":memory:")
    now = datetime(2026, 10, 1, tzinfo=UTC)
    client = FakeClient({"https://acme.se": '<a href="https://jobs.lever.co/acme">Jobs</a>'})
    settings = CompaniesSettings(redetect_after_days=7)
    [feed], detected = resolve_feeds([ACME], store, client, settings, now)
    assert (feed.ats_type, feed.ats_ref, feed.supported, detected) == ("lever", "acme", True, 1)
    _, detected = resolve_feeds([ACME], store, client, settings, now + timedelta(days=3))
    assert detected == 0 and len(client.requests) == 1
    _, detected = resolve_feeds([ACME], store, client, settings, now + timedelta(days=8))
    assert detected == 1


def _job(n, source="platsbanken"):
    return Job(
        id=make_job_id(source, str(n)),
        title=f"Job {n}",
        company="Acme",
        location="Stockholm",
        sources=[SourceRef(source=source, source_id=str(n))],
    )


def test_failed_feed_is_reported_and_not_expired():
    store = Store(":memory:")
    old = datetime.now(UTC) - timedelta(days=10)
    store.upsert_job(_job(1, "lever:acme"), now=old)
    store.upsert_job(_job(2, "teamtailor:other"), now=old)
    feed_report = CompanyCrawlReport()
    pauses = []
    from jobsearcher.companies.crawl import CompanyFeed

    feeds = [CompanyFeed(ACME, "lever", "acme")]
    assert (
        list(crawl_feeds(feeds, FakeClient({}), keep_all, feed_report, sleep=pauses.append)) == []
    )
    assert pauses == [15.0]  # retried once, after a pause
    assert feed_report.failed == ["lever:acme"]
    assert store.expire_jobs(3, skip_sources=set(feed_report.failed)) == 1
    assert store.get_job(_job(1, "lever:acme").id).status == JobStatus.OPEN
    assert store.get_job(_job(2, "teamtailor:other").id).status == JobStatus.EXPIRED


def test_run_search_includes_company_jobs():
    store = Store(":memory:")
    config = Config(search=SearchConfig(locations=["Stockholm"]))
    company = ACME.model_copy(update={"ats": AtsRef(type="varbi", ref="acme")})
    client = FakeClient({"https://acme.varbi.com/en/what:rssfeed/": VARBI_RSS})
    report = run_search(
        config, store, sources=[], keywords=[], companies=[company], company_client=client
    )
    assert (report.new, report.companies.with_feed, report.companies.jobs) == (1, 1, 1)
    [job] = store.iter_jobs()
    assert job.sources[0].source == "varbi:acme"


def test_companies_sharing_a_feed_merge():
    """A region and its hospital both point at the region's Varbi feed."""
    store = Store(":memory:")
    config = Config(search=SearchConfig(locations=["Stockholm"]))
    region = ACME.model_copy(update={"ats": AtsRef(type="varbi", ref="acme")})
    hospital = Company(
        name="Acme sjukhus", location="Stockholm", ats=AtsRef(type="varbi", ref="acme")
    )
    client = FakeClient({"https://acme.varbi.com/en/what:rssfeed/": VARBI_RSS})
    run_search(config, store, [], [], companies=[region, hospital], company_client=client)
    assert store.count_jobs() == 1


# --- crawl settings ----------------------------------------------------------------


def test_crawl_settings_set_user_agent_and_robots():
    from jobsearcher.config import CHROME_USER_AGENT, HONEST_USER_AGENT, CrawlConfig

    assert CrawlConfig().user_agent_string == HONEST_USER_AGENT
    assert CrawlConfig(user_agent="chrome").user_agent_string == CHROME_USER_AGENT
    assert CrawlConfig(user_agent="Custom/1.0").user_agent_string == "Custom/1.0"

    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers["User-Agent"]))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, text="ok")

    config = Config()
    config.crawl = CrawlConfig(user_agent="chrome", respect_robots=False)
    client = PoliteClient.from_config(config)
    client.client = httpx.Client(
        transport=httpx.MockTransport(handler), headers=client.client.headers
    )
    client._sleep = lambda s: None
    assert client.get_text("https://acme.se/karriar") == "ok"
    assert seen == [("/karriar", CHROME_USER_AGENT)]  # robots.txt never fetched

    polite = PoliteClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None
    )
    with pytest.raises(httpx.HTTPError, match="robots.txt"):
        polite.get("https://acme.se/karriar")


def test_enabled_sources_use_the_crawl_user_agent():
    from jobsearcher.config import CrawlConfig
    from jobsearcher.sources import enabled_sources

    config = Config()
    config.crawl = CrawlConfig(user_agent="Custom/1.0")
    for source in enabled_sources(config):
        assert source.client.headers["User-Agent"] == "Custom/1.0"


def test_retry_failed_redetects_only_failures():
    store = Store(":memory:")
    now = datetime(2026, 10, 1, tzinfo=UTC)
    found = Company(name="Found", website="https://found.se")
    missing = Company(name="Missing", website="https://missing.se")
    store.save_company_ats("found", "lever", "found", None, None, now)
    store.save_company_ats("missing", None, None, None, "403 Forbidden", now)
    client = FakeClient({"https://missing.se": '<a href="https://jobs.lever.co/missing">Jobs</a>'})
    settings = CompaniesSettings()
    _, detected = resolve_feeds([found, missing], store, client, settings, now)
    assert detected == 0
    feeds, detected = resolve_feeds(
        [found, missing], store, client, settings, now, retry_failed=True
    )
    assert detected == 1 and client.requests == ["https://missing.se"]
    assert [f.ats_ref for f in feeds] == ["found", "missing"]


def test_browser_transport_lets_chrome_set_headers():
    from jobsearcher.companies.http import BrowserTransport

    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "text/html", "Content-Encoding": "gzip"}
        content = b"<html>ok</html>"

    class FakeSession:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return FakeResponse()

    transport = BrowserTransport.__new__(BrowserTransport)  # skip importing curl_cffi
    transport.session = FakeSession()
    client = httpx.Client(transport=transport, headers={"User-Agent": "python", "Referer": "r"})
    response = client.get("https://acme.se/jobs")
    assert response.text == "<html>ok</html>"  # not decoded a second time as gzip
    method, url, kwargs = transport.session.calls[0]
    assert (method, url, kwargs["allow_redirects"]) == ("GET", "https://acme.se/jobs", False)
    assert {k.lower() for k in kwargs["headers"]} == {"referer"}  # Chrome sets the rest


# --- Workday ---------------------------------------------------------------------


def test_workday_ref_parsing_and_cities():
    from jobsearcher.sources.ats.workday import city_from, parse_ref

    assert parse_ref("essity.wd3.myworkdayjobs.com/en-US/Job_opportunities") == (
        "essity.wd3.myworkdayjobs.com",
        "essity",
        "Job_opportunities",
    )
    assert parse_ref("saabgroup.wd116.myworkdayjobs.com/Saab_careers/job/Linkping")[2] == (
        "Saab_careers"
    )
    with pytest.raises(ValueError):
        parse_ref("example.com/careers")
    assert city_from("Sweden, Gothenburg") == "Göteborg"
    assert city_from("Stockholm - Solna") == "Stockholm"
    assert city_from("3 Locations") is None


def test_workday_sweden_facet_flat_and_nested():
    from jobsearcher.sources.ats.workday import sweden_facet

    flat = [{"facetParameter": "Country", "values": [{"descriptor": "Sweden", "id": "se"}]}]
    nested = [
        {
            "facetParameter": "locationMainGroup",
            "values": [
                {
                    "facetParameter": "locationCountry",
                    "descriptor": "Location Country",
                    "values": [
                        {"descriptor": "Norway", "id": "no"},
                        {"descriptor": "Sweden", "id": "se2"},
                    ],
                }
            ],
        }
    ]
    assert sweden_facet(flat) == ("Country", "se")
    assert sweden_facet(nested) == ("locationCountry", "se2")
    assert sweden_facet([{"facetParameter": "jobFamily", "values": []}]) is None


class WorkdayClient:
    """Serves a fake Workday API: two pages of Swedish jobs and their details."""

    API = "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External"

    def __init__(self, facets=True):
        self.facets, self.posts, self.details = facets, [], []

    def post_json(self, url, body, headers=None):
        assert url == f"{self.API}/jobs" and headers["Origin"].endswith("myworkdayjobs.com")
        self.posts.append(body)
        if body["limit"] == 1:
            facets = [
                {"facetParameter": "Country", "values": [{"descriptor": "Sweden", "id": "se"}]}
            ]
            return {"total": 99, "facets": facets if self.facets else []}
        postings = [
            {"title": "HR Business Partner", "externalPath": "/job/Sweden-Stockholm/HRBP_R1",
             "locationsText": "Sweden, Stockholm", "bulletFields": ["R1"]},
            {"title": "Plant manager", "externalPath": "/job/Sweden-Kiruna/PM_R2",
             "locationsText": "Sweden, Kiruna", "bulletFields": ["R2"]},
            {"title": "Change lead", "externalPath": "/job/x/CL_R3",
             "locationsText": "2 Locations", "bulletFields": ["R3"]},
        ]  # fmt: skip
        return {"total": 3, "jobPostings": postings if body["offset"] == 0 else []}

    def get_json(self, url, params=None, headers=None):
        self.details.append(url)
        location = "Sweden, Gothenburg" if url.endswith("CL_R3") else "Sweden, Stockholm"
        return {
            "jobPostingInfo": {
                "jobDescription": "<p>Lead HR.</p>",
                "location": location,
                "startDate": "2026-10-01",
                "endDate": "2026-10-20",
                "timeType": "Full time",
                "externalUrl": "https://acme.wd3.myworkdayjobs.com/External"
                + url.split("External")[1],
            }
        }


def test_workday_lists_sweden_and_fetches_wanted_details():
    from jobsearcher.sources.ats import workday

    client = WorkdayClient()
    wanted = lambda job: job.location in ("Stockholm", "Göteborg")  # noqa: E731
    jobs = list(
        workday.fetch_jobs(client, "acme.wd3.myworkdayjobs.com/en-US/External", ACME, wanted)
    )
    assert [(j.title, j.location) for j in jobs] == [
        ("HR Business Partner", "Stockholm"),
        ("Change lead", "Göteborg"),  # "2 Locations": known only after its details
    ]
    assert client.posts[1]["appliedFacets"] == {"Country": ["se"]}
    assert not any(u.endswith("PM_R2") for u in client.details)  # Kiruna: no detail request
    job = jobs[0]
    assert job.description == "Lead HR." and job.deadline.date().isoformat() == "2026-10-20"
    assert job.sources[0].source == "workday:acme-wd3-myworkdayjobs-com-external"
    assert job.sources[0].source_id == "R1"

    no_facets = WorkdayClient(facets=False)  # a Sweden-only site: no country filter
    assert (
        len(
            list(workday.fetch_jobs(no_facets, "acme.wd3.myworkdayjobs.com/External", ACME, wanted))
        )
        == 2
    )
    assert no_facets.posts[1]["appliedFacets"] == {}


# --- generic JSON-LD reader ---------------------------------------------------------

AD_PAGE = """<html><head>
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [
  {"@type": "Organization", "name": "Randstad"},
  {"@type": "JobPosting", "title": "HR-partner till Acme",
   "description": "&lt;p&gt;Du blir HR-partner.&lt;/p&gt;",
   "identifier": {"@type": "PropertyValue", "value": "ad-1"},
   "datePosted": "2026-10-02T11:40:09+0000", "validThrough": "2026-10-25T12:00:00+0000",
   "employmentType": ["FULL_TIME"],
   "hiringOrganization": {"@type": "Organization", "name": "Acme AB"},
   "jobLocation": {"@type": "Place", "address": {"addressLocality": "Gothenburg",
     "addressRegion": "Västra Götaland", "addressCountry": "SE"}}}
]}</script></head><body>Ad</body></html>"""

LISTING = """<html><body>
<a href="/jobb/re-stockholms-lan/ci-stockholm/">Stockholm</a>
<a href="/jobb/hr-partner-till-acme_goteborg_08122283-ad3a-4fca-bcb1-f94a46e2064e/">HR-partner</a>
<a href="/jobb/?pg=2">Next</a>
<a href="https://other.se/jobb/123456/">Elsewhere</a>
<a href="/om-oss/">About</a>
</body></html>"""


def test_jsonld_job_postings_and_links():
    from jobsearcher.sources.ats.jsonld import job_links, job_postings

    [posting] = job_postings(AD_PAGE)
    assert posting["title"] == "HR-partner till Acme"
    assert job_postings('<script type="application/ld+json">{broken</script>') == []
    assert job_postings(
        '<script type="application/ld+json">[{"@type": "JobPosting", "title": "A"}]</script>'
    )
    links = job_links(LISTING, "https://www.randstad.se/jobb/")
    # The ad (an id in its URL) comes first; category and pagination pages are skipped.
    assert links == [
        "https://www.randstad.se/jobb/hr-partner-till-acme_goteborg_08122283-ad3a-4fca-bcb1-f94a46e2064e/"
    ]


def test_jsonld_reader_follows_ads_and_maps_postings():
    from jobsearcher.sources.ats import jsonld

    ad = "https://www.randstad.se/jobb/hr-partner-till-acme_goteborg_08122283-ad3a-4fca-bcb1-f94a46e2064e/"
    client = FakeClient({"https://www.randstad.se/jobb/": LISTING, ad: AD_PAGE})
    randstad = Company(name="Randstad")
    [job] = jsonld.fetch_jobs(client, "https://www.randstad.se/jobb/", randstad, keep_all)
    assert (job.title, job.company, job.location) == ("HR-partner till Acme", "Acme AB", "Göteborg")
    assert job.description == "Du blir HR-partner."
    assert job.deadline.date().isoformat() == "2026-10-25" and job.employment_type == "full time"
    assert job.sources[0].source_id == "ad-1" and job.url == ad


def test_jsonld_reader_gives_up_on_sites_without_job_data(monkeypatch):
    from jobsearcher.sources.ats import jsonld

    monkeypatch.setattr(jsonld, "MAX_MISSES", 3)
    links = "".join(f'<a href="/jobb/ad-{n}-12345/">Ad</a>' for n in range(10))
    pages = {f"https://acme.se/jobb/ad-{n}-12345/": "<html>no data</html>" for n in range(10)}
    client = FakeClient({"https://acme.se/jobb/": links, **pages})
    assert list(jsonld.fetch_jobs(client, "https://acme.se/jobb/", ACME, keep_all)) == []
    assert len(client.requests) == 1 + 3  # the listing, then three misses


def test_detect_falls_back_to_jsonld():
    ad = "https://www.randstad.se/jobb/hr-partner-till-acme_goteborg_08122283-ad3a-4fca-bcb1-f94a46e2064e/"
    randstad = Company(name="Randstad", careers_url="https://www.randstad.se/jobb/")
    client = FakeClient({"https://www.randstad.se/jobb/": LISTING, ad: AD_PAGE})
    result = detect(randstad, client)
    assert (result.ats_type, result.ats_ref) == ("jsonld", "https://www.randstad.se/jobb/")


# --- SuccessFactors, ReachMee, Jobylon ---------------------------------------------

SF_RSS = """<?xml version="1.0" encoding="UTF-8" ?><rss version='2.0'><channel>
<item><title>HR Partner (Södertälje, AB, 151 87)</title>
<description>&lt;p&gt;Partner HR at Scania.&lt;/p&gt;</description>
<pubDate>Mon, 05 Oct 2026 2:00:00 GMT</pubDate>
<link>https://jobs.scania.com/job/Sodertalje-HR-Partner/1413706233/?feedId=null&amp;utm_source=J2WRSS</link></item>
<item><title>Plant manager (Kosice, SK, 040 01)</title><link>https://jobs.scania.com/job/x/1413706234/</link></item>
<item><title>Service Manager (Luleå, Other/Not Applicable, Sweden)</title><link>https://jobs.scania.com/job/y/1413706235/</link></item>
<item><title>Controller</title><link>https://jobs.scania.com/job/z/1413706236/</link></item>
</channel></rss>"""


def test_successfactors_feed():
    from jobsearcher.sources.ats import successfactors

    client = FakeClient(
        {"https://jobs.scania.com/services/rss/job/?locale=en_US&rows=3000": SF_RSS}
    )
    scania = Company(name="Scania", location="Södertälje")
    jobs = list(
        successfactors.fetch_jobs(client, "https://jobs.scania.com/go/x/1/", scania, keep_all)
    )
    assert [(j.title, j.location) for j in jobs] == [
        ("HR Partner", "Södertälje"),
        ("Service Manager", "Luleå"),
        ("Controller", "Södertälje"),  # no place in the title: the company's location
    ]  # Kosice (Slovakia) is dropped
    job = jobs[0]
    assert job.description == "Partner HR at Scania." and job.url.endswith("/1413706233/")
    assert job.sources[0].source_id == "1413706233"
    with pytest.raises(ValueError, match="careers_url"):
        successfactors.base_url("performancemanager5.successfactors.eu/verp/x.js")


REACHMEE_MAIN = """<table><thead><tr><th>Tjänst</th><th>Publicerat</th><th>Sista ansökningsdag</th>
<th>Ort</th><th>Län</th></tr></thead><tbody>
<tr><td><a href='https://web103.reachmee.com/ext/I017/653/job?site=17&lang=SE&validator=abc&job_id=25659' class='btn'>HR-specialist</a></td>
<td><span style="display:none">2026-10-02</span> 2026-10-02</td><td><span>2026-10-31</span> 2026-10-31</td>
<td>Solna</td><td>Stockholms län</td></tr>
<tr><td><a href='https://web103.reachmee.com/ext/I017/653/job?site=17&lang=SE&validator=abc&job_id=25660'>Byggledare</a></td>
<td>2026-10-01</td><td>2026-10-30</td><td>Kiruna</td><td>Norrbottens län</td></tr>
</tbody></table>"""


def test_reachmee_table_and_ad():
    from jobsearcher.sources.ats import reachmee

    ref = "web103.reachmee.com/ext/I017/653/policy?site=17&amp;lang=SE&amp;validator=abc&amp;ihelper=x"
    main = "https://web103.reachmee.com/ext/I017/653/main?site=17&lang=SE&validator=abc"
    detail = (
        "https://web103.reachmee.com/ext/I017/653/job?site=17&lang=SE&validator=abc&job_id=25659"
    )
    client = FakeClient(
        {main: REACHMEE_MAIN, detail: '<div class="jobad-body"><p>Du stöttar chefer.</p></div>'}
    )
    sweco = Company(name="Sweco")
    wanted = lambda job: job.location == "Solna"  # noqa: E731
    [job] = reachmee.fetch_jobs(client, ref, sweco, wanted)
    assert (job.title, job.location, job.region) == ("HR-specialist", "Solna", "Stockholms län")
    assert job.deadline.date().isoformat() == "2026-10-31"
    assert job.description == "Du stöttar chefer." and job.sources[0].source_id == "25659"
    assert not any(r.endswith("25660") for r in client.requests)  # unwanted: no detail
    with pytest.raises(ValueError, match="site/validator"):
        reachmee.main_url("web103.reachmee.com/ext/I017/653/policy")


JOBYLON_WIDGET = """<script>var jobs = [
{ id: '366341', url: '/jobs/366341-acme-hr-partner/', title: 'HR-partner till Acme', company: 'Acme AB',
  company_id: '1815', locations_text: 'Solna, Göteborg', employment_type: 'Heltid',
  to_date: '15 november 2026', published_date: '15 september 2026', language: 'Swedish' },
{ id: '366342', url: '/jobs/366342-acme-o-neill/', title: 'Lagerchef \\u0026 planerare', company: 'Acme AB',
  locations_text: 'Kiruna', to_date: '', published_date: '1 oktober 2026' },
];</script>"""


def test_jobylon_widget_company_id_and_ad():
    from jobsearcher.sources.ats import jobylon

    client = FakeClient(
        {
            "https://emp.jobylon.com/jobs/285413/": '<a href="/companies/1815/terms/">Terms</a>',
            "https://cdn.jobylon.com/jobs/companies/1815/embed/v2/": JOBYLON_WIDGET,
            "https://emp.jobylon.com/jobs/366341-acme-hr-partner/": (
                "<style>.canvas-job-description { x }</style>"
                '<div class="canvas-job-description"><p>Leda HR-arbete.</p></div>'
            ),
        }
    )
    ref = "emp.jobylon.com/applications/jobs/285413/create"
    assert jobylon.company_id(client, ref) == "1815"
    assert jobylon.company_id(client, "media-eu.jobylon.com/assets/companies/2062/x.mp4") == "2062"
    wanted = lambda job: job.location == "Solna"  # noqa: E731
    [job] = jobylon.fetch_jobs(client, ref, ACME, wanted)
    assert (job.title, job.company, job.location) == ("HR-partner till Acme", "Acme AB", "Solna")
    assert job.deadline.date().isoformat() == "2026-11-15"
    assert job.published_at.date().isoformat() == "2026-09-15"
    assert job.description == "Leda HR-arbete."
    assert jobylon.parse_widget(JOBYLON_WIDGET)[1]["title"] == "Lagerchef & planerare"
    assert jobylon.swedish_date("3 maj 2026").month == 5 and jobylon.swedish_date("") is None


def test_county_fills_in_for_the_location_filter():
    from jobsearcher.pipeline import matches_filters
    from jobsearcher.places import county_of

    assert (
        county_of("Södertälje") == "Stockholms län"
        and county_of("Mölndal") == "Västra Götalands län"
    )
    assert county_of("Kiruna") is None
    search = SearchConfig(locations=["Stockholm"], include_remote=False)
    job = Job(id="1", title="HR", location="Södertälje", sources=[])
    assert matches_filters(job, search)  # via Stockholms län, as Platsbanken jobs are
    assert not matches_filters(job.model_copy(update={"location": "Kiruna"}), search)


def test_detection_records_new_ats_refs():
    sf_page = '<script src="https://performancemanager5.successfactors.eu/verp/jquery.js"></script>'
    assert find_ats(sf_page, "https://jobs.scania.com/go/Jobs/9096601/") == (
        "successfactors",
        "https://jobs.scania.com",
    )
    rm = '<a href="https://web103.reachmee.com/ext/I017/653/policy?site=17&amp;validator=abc">x</a>'
    assert find_ats(rm, "https://sweco.se") == (
        "reachmee",
        "web103.reachmee.com/ext/I017/653/policy?site=17&validator=abc",
    )


def test_feed_retried_once_after_a_transient_error(monkeypatch):
    """A one-off 400 (seen from Teamtailor for Nobina) shouldn't cost the whole feed."""
    from jobsearcher.companies import crawl as crawl_module
    from jobsearcher.companies.crawl import CompanyFeed

    calls = []

    def flaky(client, ref, company, wanted):
        calls.append(len(calls) + 1)
        yield _job(1, "lever:acme")
        if len(calls) == 1:
            raise httpx.HTTPStatusError(
                "400", request=httpx.Request("GET", "https://x"), response=httpx.Response(400)
            )
        yield _job(2, "lever:acme")

    monkeypatch.setitem(crawl_module.FETCHERS, "lever", flaky)
    feeds = [CompanyFeed(ACME, "lever", "acme")]
    report, pauses = CompanyCrawlReport(), []
    jobs = list(crawl_feeds(feeds, FakeClient({}), keep_all, report, sleep=pauses.append))
    assert [j.title for j in jobs] == ["Job 1", "Job 2"]  # job 1 isn't yielded twice
    assert (len(calls), pauses, report.failed, report.jobs) == (2, [15.0], [], 2)

    def broken(client, ref, company, wanted):
        calls.append(0)
        raise httpx.ConnectError("down")
        yield  # pragma: no cover

    monkeypatch.setitem(crawl_module.FETCHERS, "lever", broken)
    report = CompanyCrawlReport()
    assert list(crawl_feeds(feeds, FakeClient({}), keep_all, report, sleep=lambda s: None)) == []
    assert report.failed == ["lever:acme"] and calls[-2:] == [0, 0]  # tried twice, then gave up


def test_malformed_xml_feed_fails_alone():
    """A Varbi feed once answered with broken XML; that crashed the whole daemon."""
    from jobsearcher.companies.crawl import CompanyFeed

    company = ACME.model_copy(update={"ats": AtsRef(type="varbi", ref="acme")})
    client = FakeClient({"https://acme.varbi.com/en/what:rssfeed/": "<rss><channel></rss>"})
    feeds = [CompanyFeed(company, "varbi", "acme")]
    report = CompanyCrawlReport()
    assert list(crawl_feeds(feeds, client, keep_all, report, sleep=lambda s: None)) == []
    assert report.failed == ["varbi:acme"]


def test_workday_multi_location_jobs_use_a_swedish_additional_location():
    """Xylem's "Senior Project Manager ... Global" is based in Herford (Germany) and also
    open in Sundbyberg: the location filter must see Sundbyberg, not only Herford."""
    from jobsearcher.pipeline import matches_filters
    from jobsearcher.sources.ats import workday

    def info(location, extra=None):
        data = {"location": location, "jobDescription": "<p>Lead.</p>"}
        return {"jobPostingInfo": data | ({"additionalLocations": extra} if extra else {})}

    base = Job(
        id="1",
        title="Senior Project Manager Commercial Excellence",
        sources=[SourceRef(source="workday:x", source_id="1")],
    )
    search = SearchConfig(locations=["Stockholm", "Göteborg"], include_remote=False)

    herford = workday.with_details(base, info("Herford", ["Nanterre, France", "Sundbyberg"]))
    assert herford.location == "Sundbyberg" and matches_filters(herford, search)
    # Nothing Swedish among the extras: the main (foreign) location stays and it's dropped.
    abroad = workday.with_details(base, info("Herford", ["Nanterre, France", "Madrid"]))
    assert abroad.location == "Herford" and not matches_filters(abroad, search)
    # A Swedish main location isn't replaced by an extra one.
    sweden = workday.with_details(base, info("Sweden, Göteborg", ["Sundbyberg"]))
    assert sweden.location == "Göteborg"
    # A single string, or a county town outside the city list (Södertälje, Stockholms län).
    one = workday.with_details(base, info("Herford", "Södertälje"))
    assert one.location == "Södertälje" and matches_filters(one, search)
