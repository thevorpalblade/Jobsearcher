"""SmartRecruiters: `https://api.smartrecruiters.com/v1/companies/<ref>/postings?country=se`
lists postings in Sweden; the ad text needs one extra request per posting, so that's
only fetched for postings the pipeline would keep."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from jobsearcher.companies.config import Company
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.sources.ats.common import AtsClient, Wanted, feed_source, html_to_text
from jobsearcher.sources.base import parse_datetime

API = "https://api.smartrecruiters.com/v1/companies/{ref}/postings"
PAGE_SIZE = 100
MAX_OFFSET = 2000


def fetch_jobs(client: AtsClient, ref: str, company: Company, wanted: Wanted) -> Iterator[Job]:
    offset = 0
    while offset < MAX_OFFSET:
        data: Any = client.get_json(
            API.format(ref=ref), {"country": "se", "limit": PAGE_SIZE, "offset": offset}
        )
        postings = data.get("content") or []
        for posting in postings:
            job = parse_posting(posting, company, feed_source("smartrecruiters", ref))
            if job is None or not wanted(job):
                continue
            detail: Any = client.get_json(f"{API.format(ref=ref)}/{posting['id']}")
            yield with_details(job, detail)
        offset += len(postings)
        if not postings or offset >= (data.get("totalFound") or 0):
            return


def parse_posting(posting: dict[str, Any], company: Company, source: str) -> Job | None:
    title, posting_id = posting.get("name"), posting.get("id")
    if not title or not posting_id:
        return None
    loc = posting.get("location") or {}
    if (loc.get("country") or "se").lower() != "se":
        return None
    source_id = str(posting_id)
    return Job(
        id=make_job_id(source, source_id),
        title=title.strip(),
        company=company.name,
        company_org_nr=company.org_nr,
        location=loc.get("city"),
        region=loc.get("region"),
        remote=bool(loc.get("remote")) or None,
        employment_type=(posting.get("typeOfEmployment") or {}).get("label"),
        published_at=parse_datetime(posting.get("releasedDate")),
        sources=[SourceRef(source=source, source_id=source_id)],
    )


def with_details(job: Job, detail: dict[str, Any]) -> Job:
    sections = ((detail.get("jobAd") or {}).get("sections")) or {}
    order = ("jobDescription", "qualifications", "additionalInformation", "companyDescription")
    parts = []
    for key in order:
        section = sections.get(key) or {}
        text = html_to_text(section.get("text"))
        if text:
            parts.append(f"{section.get('title') or ''}\n{text}".strip())
    url = detail.get("postingUrl")
    job = job.model_copy(
        update={
            "description": "\n\n".join(parts),
            "url": url,
            "apply_url": detail.get("applyUrl") or url,
        }
    )
    job.sources[0].url = url
    return job
