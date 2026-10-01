"""Greenhouse job boards: `https://boards-api.greenhouse.io/v1/boards/<ref>/jobs?content=true`
lists every open job with its full description. Locations are free text, so a job is
kept only when its location or offices mention Sweden or a Swedish city."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from jobsearcher.companies.config import Company
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.sources.ats.common import AtsClient, Wanted, html_to_text, is_sweden, swedish_city
from jobsearcher.sources.base import parse_datetime

API = "https://boards-api.greenhouse.io/v1/boards/{ref}/jobs"


def fetch_jobs(client: AtsClient, ref: str, company: Company, wanted: Wanted) -> Iterator[Job]:
    data: Any = client.get_json(API.format(ref=ref), {"content": "true"})
    for job in data.get("jobs") or []:
        parsed = parse_job(job, company)
        if parsed is not None:
            yield parsed


def parse_job(job: dict[str, Any], company: Company) -> Job | None:
    title, url = job.get("title"), job.get("absolute_url")
    if not title or not url or not job.get("id"):
        return None
    texts = [(job.get("location") or {}).get("name") or ""]
    texts += [
        " ".join(filter(None, [o.get("name"), o.get("location")]))
        for o in job.get("offices") or []
        if isinstance(o, dict)
    ]
    swedish = [t for t in texts if is_sweden(None, t) or swedish_city(t)]
    if not swedish:
        return None
    location = swedish_city(swedish[0]) or swedish[0]
    source_id = str(job["id"])
    source = f"greenhouse:{company.slug}"
    return Job(
        id=make_job_id(source, source_id),
        title=title.strip(),
        company=company.name,
        company_org_nr=company.org_nr,
        location=location,
        remote="remote" in swedish[0].lower() or None,
        description=html_to_text(job.get("content")),
        url=url,
        apply_url=url,
        published_at=parse_datetime(job.get("first_published") or job.get("updated_at")),
        deadline=parse_datetime(job.get("application_deadline")),
        sources=[SourceRef(source=source, source_id=source_id, url=url)],
    )
