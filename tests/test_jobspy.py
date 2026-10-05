import math
from datetime import UTC, date, datetime, timedelta

from jobsearcher.config import Config, JobSpyConfig
from jobsearcher.models import Job, JobStatus, SourceRef, make_job_id
from jobsearcher.pipeline import run_search
from jobsearcher.sources import enabled_sources
from jobsearcher.sources.jobspy_source import JobSpySource, parse_row, split_location
from jobsearcher.store import Store

NAN = math.nan


def _row(n, location="Gothenburg, Västra Götaland County, Sweden", **extra):
    return {
        "id": f"li-{n}",
        "site": "linkedin",
        "job_url": f"https://www.linkedin.com/jobs/view/{n}",
        "job_url_direct": NAN,
        "title": f"HR Business Partner {n}",
        "company": "Acme AB",
        "location": location,
        "date_posted": date(2026, 9, 30),
        "job_type": "fulltime",
        "is_remote": False,
        "emails": NAN,
        "description": "**Om rollen**\nKontakta anna.andersson@acme.se.",
        **extra,
    }


def test_parse_row_maps_and_normalises():
    job = parse_row(_row(1), "linkedin")
    assert (job.location, job.region) == ("Göteborg", "Västra Götaland")
    assert job.published_at == datetime(2026, 9, 30, tzinfo=UTC)
    assert job.apply_url == job.url == "https://www.linkedin.com/jobs/view/1"
    assert job.remote is None and job.employment_type == "fulltime"
    assert job.sources == [SourceRef(source="linkedin", source_id="li-1", url=job.url)]
    assert job.id == make_job_id("linkedin", "li-1")
    [contact] = job.contacts
    assert (contact.email, contact.provenance) == ("anna.andersson@acme.se", "linkedin:ad_text")
    assert parse_row({**_row(2), "title": NAN}, "linkedin") is None
    assert split_location("Stockholm, Stockholm County, Sweden") == ("Stockholm", "Stockholm")
    assert split_location("Sweden") == ("Sweden", None)
    assert split_location(None) == (None, None)


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


def test_source_searches_with_settings_and_pauses():
    calls, sleeps = [], []

    def scrape(**kwargs):
        calls.append(kwargs)
        return FakeFrame([_row(len(calls))])

    settings = JobSpyConfig(sites=["linkedin"], hours_old=72, results_per_search=5, pause_s=3)
    source = JobSpySource("linkedin", settings, scrape=scrape, sleep=sleeps.append)
    jobs = [*source.search("HRBP"), *source.search("change manager")]
    assert [j.title for j in jobs] == ["HR Business Partner 1", "HR Business Partner 2"]
    assert calls[0] | {"search_term": None} == calls[1] | {"search_term": None}
    assert calls[0]["site_name"] == ["linkedin"] and calls[0]["hours_old"] == 72
    assert calls[0]["results_wanted"] == 5 and calls[0]["location"] == "Sweden"
    assert sleeps == [3]  # only between searches


def test_enabled_sources_skip_jobspy_when_not_installed(monkeypatch, caplog):
    import jobsearcher.sources.jobspy_source as module

    config = Config()
    config.sources.jobspy = JobSpyConfig(sites=["linkedin", "indeed"])
    monkeypatch.setattr(module, "available", lambda: True)
    assert [s.name for s in enabled_sources(config)][-2:] == ["linkedin", "indeed"]
    monkeypatch.setattr(module, "available", lambda: False)
    assert "linkedin" not in [s.name for s in enabled_sources(config)]
    assert "pip install '.[jobspy]'" in caplog.text


def _job(n, source, published):
    return Job(
        id=make_job_id(source, str(n)),
        title=f"Job {n}",
        company="Acme",
        location="Stockholm",
        published_at=published,
        sources=[SourceRef(source=source, source_id=str(n))],
    )


def test_jobspy_jobs_expire_by_age_not_by_absence():
    store = Store(":memory:")
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=10)
    recent = _job(1, "linkedin", now - timedelta(days=5))  # unseen for 10 days, still young
    old = _job(2, "linkedin", now - timedelta(days=40))
    board = _job(3, "platsbanken", now - timedelta(days=5))  # unseen: expires as before
    for job in (recent, old, board):
        store.upsert_job(job, now=long_ago)
    config = Config()
    config.sources.jobspy = JobSpyConfig(sites=["linkedin"], max_age_days=30)
    report = run_search(config, store, sources=[], keywords=[], companies=[])
    assert report.expired == 2
    status = {j.id: j.status for j in store.iter_jobs(status=None)}
    assert status == {
        recent.id: JobStatus.OPEN,
        old.id: JobStatus.EXPIRED,
        board.id: JobStatus.EXPIRED,
    }
