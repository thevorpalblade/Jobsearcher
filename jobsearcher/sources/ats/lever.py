"""Lever: `https://api.lever.co/v0/postings/<ref>?mode=json` lists every open posting."""

from __future__ import annotations

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
    is_sweden,
    swedish_city,
)

API = "https://api.lever.co/v0/postings/{ref}"


def fetch_jobs(client: AtsClient, ref: str, company: Company, wanted: Wanted) -> Iterator[Job]:
    postings: Any = client.get_json(API.format(ref=ref), {"mode": "json"})
    for posting in postings or []:
        job = parse_posting(posting, company, feed_source("lever", ref))
        if job is not None:
            yield job


def parse_posting(posting: dict[str, Any], company: Company, source: str) -> Job | None:
    title, url = posting.get("text"), posting.get("hostedUrl")
    if not title or not url or not posting.get("id"):
        return None
    categories = posting.get("categories") or {}
    locations = categories.get("allLocations") or [categories.get("location")]
    locations = [loc for loc in locations if loc]
    # A posting has one country but may list several offices (e.g. London and
    # Stockholm): keep it if it's in Sweden or names a Swedish office we filter on.
    location = categories.get("location")
    if not is_sweden(posting.get("country")):
        location = next(
            (loc for loc in locations if is_sweden(None, loc) or swedish_city(loc)), None
        )
        if location is None:
            return None

    parts = [posting.get("descriptionPlain") or ""]
    for block in posting.get("lists") or []:
        items = block.get("content") or ""
        parts.append(f"{block.get('text', '')}\n{html_to_text(items)}")
    parts.append(posting.get("additionalPlain") or "")
    created = posting.get("createdAt")
    return Job(
        id=make_job_id(source, posting["id"]),
        title=title.strip(),
        company=company.name,
        company_org_nr=company.org_nr,
        location=location,
        remote=posting.get("workplaceType") == "remote" or None,
        description="\n\n".join(p.strip() for p in parts if p.strip()),
        employment_type=categories.get("commitment"),
        url=url,
        apply_url=posting.get("applyUrl") or url,
        published_at=datetime.fromtimestamp(created / 1000, UTC) if created else None,
        sources=[SourceRef(source=source, source_id=posting["id"], url=url)],
    )
