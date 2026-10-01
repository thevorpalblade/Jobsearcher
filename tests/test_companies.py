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
        SourceRef(source="teamtailor:acme-sverige-ab", source_id="uuid-1", url=job.url)
    ]
    assert job.id == make_job_id("teamtailor:acme-sverige-ab", "uuid-1")


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
    store.upsert_job(_job(1, "lever:acme-sverige-ab"), now=old)
    store.upsert_job(_job(2, "teamtailor:other"), now=old)
    feed_report = CompanyCrawlReport()
    from jobsearcher.companies.crawl import CompanyFeed

    feeds = [CompanyFeed(ACME, "lever", "acme")]
    assert list(crawl_feeds(feeds, FakeClient({}), keep_all, feed_report)) == []
    assert feed_report.failed == ["lever:acme-sverige-ab"]
    assert store.expire_jobs(3, skip_sources=set(feed_report.failed)) == 1
    assert store.get_job(_job(1, "lever:acme-sverige-ab").id).status == JobStatus.OPEN
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
    assert job.sources[0].source == "varbi:acme-sverige-ab"
