"""Jobylon (LKAB, Coor, Unilabs, Kronans Apotek): the company's job widget
(`cdn.jobylon.com/jobs/companies/<id>/embed/v2/`) carries every job as inline
JavaScript objects (title, URL, place, dates). The ad text is on each job's page,
fetched only for wanted jobs. `ref` is the company id, or any Jobylon URL detection
found (a job, the company's jobylon.com site, a media file) that leads to it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from jobsearcher.companies.config import Company
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.sources.ats.common import (
    AtsClient,
    Wanted,
    feed_source,
    html_to_text,
    swedish_city,
)

SITE = "https://emp.jobylon.com"
WIDGET = "https://cdn.jobylon.com/jobs/companies/{id}/embed/v2/"
_JOB_START = re.compile(r"\{\s*id:\s*'(\d+)',")
_FIELD = re.compile(
    r"\b(url|title|company|locations_text|employment_type|to_date|published_date|language):\s*'((?:[^'\\]|\\.)*)'"
)
_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("januari", "january"), ("februari", "february"), ("mars", "march"),
            ("april",), ("maj", "may"), ("juni", "june"), ("juli", "july"),
            ("augusti", "august"), ("september",), ("oktober", "october"),
            ("november",), ("december",),
        ]
    )
    for m in names
}  # fmt: skip


def company_id(client: AtsClient, ref: str) -> str:
    """The Jobylon company id behind a detected ref."""
    if ref.isdigit():
        return ref
    match = re.search(r"companies/(\d+)", ref)
    if match:
        return match.group(1)
    job = re.search(r"jobs/(\d+)", ref)
    url = f"{SITE}/jobs/{job.group(1)}/" if job else ("https://" + ref.split("://")[-1])
    match = re.search(r"companies/(\d+)", client.get_text(url))
    if not match:
        raise ValueError(f"No Jobylon company id found behind {ref!r}")
    return match.group(1)


def _unescape(value: str) -> str:
    value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), value)
    return value.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


def parse_widget(page: str) -> list[dict[str, str]]:
    """The jobs in a widget page, as {"id", "url", "title", ...} dicts."""
    starts = [(m.start(), m.group(1)) for m in _JOB_START.finditer(page)]
    jobs = []
    for i, (pos, job_id) in enumerate(starts):
        block = page[pos : starts[i + 1][0] if i + 1 < len(starts) else len(page)]
        fields = {k: _unescape(v) for k, v in _FIELD.findall(block)}
        if fields.get("url") and fields.get("title"):
            jobs.append({"id": job_id, **fields})
    return jobs


def swedish_date(value: str | None) -> datetime | None:
    """ "15 november 2026" (Swedish or English month) -> datetime."""
    match = re.match(r"(\d{1,2})\s+([a-zåäö]+)\s+(\d{4})", (value or "").strip().lower())
    if not match or match.group(2) not in _MONTHS:
        return None
    day, month, year = int(match.group(1)), _MONTHS[match.group(2)], int(match.group(3))
    return datetime(year, month, day, tzinfo=UTC)


def fetch_jobs(client: AtsClient, ref: str, company: Company, wanted: Wanted) -> Iterator[Job]:
    cid = company_id(client, ref)
    source = feed_source("jobylon", cid)
    for item in parse_widget(client.get_text(WIDGET.format(id=cid))):
        job = to_job(item, company, source)
        if job is None or not wanted(job):
            continue
        yield job.model_copy(update={"description": ad_text(client.get_text(job.url))})  # type: ignore[arg-type]


MAX_AD_CHARS = 10_000


def ad_text(page: str) -> str:
    # The element, not the stylesheet rules that mention the same class earlier on.
    start = page.find('class="canvas-job-description')
    if start < 0:
        return ""
    start = page.find(">", start) + 1
    return html_to_text(page[start:])[:MAX_AD_CHARS]


def to_job(item: dict[str, Any], company: Company, source: str) -> Job | None:
    url = SITE + item["url"] if item["url"].startswith("/") else item["url"]
    places = item.get("locations_text") or ""
    return Job(
        id=make_job_id(source, item["id"]),
        title=" ".join(item["title"].split()),
        # One widget can list several group companies.
        company=item.get("company") or company.name,
        location=swedish_city(places) or places.split(",")[0].strip() or company.location,
        employment_type=item.get("employment_type") or None,
        url=url,
        apply_url=url,
        published_at=swedish_date(item.get("published_date")),
        deadline=swedish_date(item.get("to_date")),
        sources=[SourceRef(source=source, source_id=item["id"], url=url)],
    )
