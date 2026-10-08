import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.ranking.ranker import JobAssessment

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """Run every test from an empty directory, so default relative paths
    (config.yaml, ranking.yaml, companies.yaml, data/) never pick up the
    developer's personal files or trigger live crawling."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _no_contact_lookups_online(monkeypatch):
    """Contact lookups (before drafts, in the daily run) would crawl real websites:
    tests that want one pass their own fake clients to contacts.service.lookup."""

    def offline(config, store):
        raise RuntimeError("no network in tests: pass clients= to contacts.service.lookup")

    monkeypatch.setattr("jobsearcher.contacts.service.make_clients", offline)


@pytest.fixture
def load_fixture():
    return lambda name: json.loads((FIXTURES / name).read_text())


def make_job(n, title, text="", published_day=1, **fields):
    """A Platsbanken job; extra keyword arguments set other Job fields."""
    return Job(
        id=make_job_id("platsbanken", str(n)),
        title=title,
        company=fields.pop("company", f"Company {n}"),
        location=fields.pop("location", "Stockholm"),
        description=text,
        published_at=datetime(2026, 9, published_day, tzinfo=UTC),
        sources=fields.pop("sources", [SourceRef(source="platsbanken", source_id=str(n))]),
        **fields,
    )


def make_assessment(
    fit=80, success=60, contacts=(), language="sv", swedish="not_mentioned", **fields
):
    data = {
        "matched_role": "HR Business Partner",
        "matched_requirements": ["HR partnering"],
        "missing_requirements": [],
        "red_flags": [],
        "rationale": "Good match.",
    } | fields
    return JobAssessment(
        fit_score=fit,
        success_score=success,
        language=language,
        swedish=swedish,
        contact_persons=list(contacts),
        **data,
    )
