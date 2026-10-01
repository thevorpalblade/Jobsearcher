"""Platsbanken via Arbetsförmedlingen's JobTech JobSearch API.

Docs: https://jobsearch.api.jobtechdev.se/  (open API, no key required)
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx

from jobsearcher.contacts import extract_contacts_from_text
from jobsearcher.models import Contact, Job, SourceRef, make_job_id
from jobsearcher.sources.base import dig, get_json, make_client, parse_datetime

log = logging.getLogger(__name__)

BASE_URL = "https://jobsearch.api.jobtechdev.se"
PAGE_SIZE = 100  # API maximum
MAX_OFFSET = 2000  # API maximum; narrow the query if a keyword hits this
AD_URL = "https://arbetsformedlingen.se/platsbanken/annonser/{id}"


class PlatsbankenSource:
    name = "platsbanken"

    def __init__(self, client: httpx.Client | None = None, base_url: str = BASE_URL):
        self.client = client or make_client()
        self.base_url = base_url.rstrip("/")

    def search(self, keyword: str, published_after: datetime | None = None) -> Iterator[Job]:
        params: dict[str, Any] = {"q": keyword, "limit": PAGE_SIZE}
        if published_after:
            # The API expects a naive local timestamp without offset.
            params["published-after"] = published_after.strftime("%Y-%m-%dT%H:%M:%S")
        offset = 0
        while offset < MAX_OFFSET:
            data = get_json(self.client, f"{self.base_url}/search", {**params, "offset": offset})
            hits = data.get("hits") or []
            for hit in hits:
                job = parse_hit(hit)
                if job is not None:
                    yield job
            total = dig(data, "total", "value") or 0
            offset += len(hits)
            if not hits or offset >= total:
                return
        log.warning("Platsbanken: %r has more than %d hits; results truncated", keyword, MAX_OFFSET)


def parse_hit(hit: dict[str, Any]) -> Job | None:
    ad_id = str(hit.get("id") or "")
    title = hit.get("headline")
    if not ad_id or not title or hit.get("removed"):
        return None

    address = hit.get("workplace_address") or {}
    location = address.get("city") or address.get("municipality")
    description = dig(hit, "description", "text") or ""

    contacts = [
        Contact(
            name=c.get("name"),
            role=c.get("description") or c.get("contact_type"),
            email=c.get("email"),
            phone=c.get("telephone"),
            provenance="platsbanken:application_contacts",
        )
        for c in hit.get("application_contacts") or []
        if isinstance(c, dict) and (c.get("name") or c.get("email") or c.get("telephone"))
    ]
    contacts += extract_contacts_from_text(description, provenance="platsbanken:ad_text")

    url = hit.get("webpage_url") or AD_URL.format(id=ad_id)
    salary = hit.get("salary_description") or dig(hit, "salary_type", "label")

    return Job(
        id=make_job_id("platsbanken", ad_id),
        title=title.strip(),
        company=dig(hit, "employer", "name") or dig(hit, "employer", "workplace"),
        company_org_nr=dig(hit, "employer", "organization_number"),
        location=location,
        region=address.get("region"),
        remote=_remote(hit, description),
        description=description,
        employment_type=dig(hit, "employment_type", "label"),
        occupation_field=dig(hit, "occupation_field", "label"),
        occupation_group=dig(hit, "occupation_group", "label"),
        salary=salary,
        url=url,
        apply_url=dig(hit, "application_details", "url") or url,
        apply_email=dig(hit, "application_details", "email"),
        published_at=parse_datetime(hit.get("publication_date")),
        deadline=parse_datetime(hit.get("application_deadline")),
        contacts=contacts,
        sources=[SourceRef(source="platsbanken", source_id=ad_id, url=url)],
    )


def _remote(hit: dict[str, Any], description: str) -> bool | None:
    flag = hit.get("remote_work")
    if isinstance(flag, bool):
        return flag
    text = description.lower()
    if "distansarbete" in text or "på distans" in text or "fully remote" in text:
        return True
    return None
