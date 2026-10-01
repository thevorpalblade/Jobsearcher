"""Teamtailor careers sites: `<site>/jobs.json`, a JSON Feed whose items carry a
schema.org JobPosting. `ref` is the careers site's base URL."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from jobsearcher.companies.config import Company
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.sources.ats.common import (
    AtsClient,
    Wanted,
    feed_source,
    html_to_text,
    is_sweden,
)
from jobsearcher.sources.base import parse_datetime

MAX_PAGES = 20


def fetch_jobs(client: AtsClient, ref: str, company: Company, wanted: Wanted) -> Iterator[Job]:
    source = feed_source("teamtailor", ref.rstrip("/"))
    url: str | None = ref.rstrip("/") + "/jobs.json"
    for _ in range(MAX_PAGES):
        if not url:
            return
        feed: Any = client.get_json(url)
        for item in feed.get("items") or []:
            job = parse_item(item, company, source)
            if job is not None:
                yield job
        url = feed.get("next_url")


def parse_item(item: dict[str, Any], company: Company, source: str) -> Job | None:
    posting = item.get("_jobposting") or {}
    title = item.get("title") or posting.get("title")
    job_url = item.get("url")
    if not title or not job_url:
        return None
    source_id = str(item.get("id") or job_url)

    places = posting.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    addresses = [p.get("address") or {} for p in places if isinstance(p, dict)]
    swedish = [a for a in addresses if is_sweden(a.get("addressCountry"))]
    if addresses and not swedish and any(a.get("addressCountry") for a in addresses):
        return None  # only offices abroad
    address = (swedish or addresses or [{}])[0]
    remote = posting.get("jobLocationType") == "TELECOMMUTE" or None

    return Job(
        id=make_job_id(source, source_id),
        title=title.strip(),
        company=company.name,
        company_org_nr=company.org_nr,
        location=address.get("addressLocality") or company.location,
        region=address.get("addressRegion"),
        remote=remote,
        description=html_to_text(posting.get("description") or item.get("content_html")),
        employment_type=_employment_type(posting.get("employmentType")),
        url=job_url,
        apply_url=job_url,
        published_at=parse_datetime(posting.get("datePosted") or item.get("date_published")),
        deadline=parse_datetime(posting.get("validThrough")),
        sources=[SourceRef(source=source, source_id=source_id, url=job_url)],
    )


def _employment_type(value: Any) -> str | None:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return str(value).replace("_", " ").lower() if value else None
