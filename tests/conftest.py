import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.ranking.ranker import JobAssessment

FIXTURES = Path(__file__).parent / "fixtures"


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
        sources=[SourceRef(source="platsbanken", source_id=str(n))],
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
