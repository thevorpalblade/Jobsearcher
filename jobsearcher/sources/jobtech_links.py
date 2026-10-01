"""JobTech Links: ads collected by Arbetsförmedlingen from other Swedish job sites.

Docs: https://links.api.jobtechdev.se/  (open API, no key required)

Links ads only carry a short `brief`, not the full text, and usually no contacts.
When the same job also exists in Platsbanken, the store merges the two records.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx

from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.sources.base import dig, get_json, make_client, parse_datetime

BASE_URL = "https://links.api.jobtechdev.se"
PAGE_SIZE = 100
MAX_OFFSET = 2000


class JobTechLinksSource:
    name = "jobtech_links"

    def __init__(self, client: httpx.Client | None = None, base_url: str = BASE_URL):
        self.client = client or make_client()
        self.base_url = base_url.rstrip("/")

    def search(self, keyword: str, published_after: datetime | None = None) -> Iterator[Job]:
        # The Links API has no reliable published-after filter; filter client-side.
        offset = 0
        while offset < MAX_OFFSET:
            data = get_json(
                self.client,
                f"{self.base_url}/joblinks",
                {"q": keyword, "limit": PAGE_SIZE, "offset": offset},
            )
            hits = data.get("hits") or []
            for hit in hits:
                job = parse_hit(hit)
                if job is None:
                    continue
                if published_after and job.published_at and job.published_at < published_after:
                    continue
                yield job
            total = dig(data, "total", "value") or 0
            offset += len(hits)
            if not hits or offset >= total:
                return


def parse_hit(hit: dict[str, Any]) -> Job | None:
    ad_id = str(hit.get("id") or "")
    title = hit.get("headline")
    if not ad_id or not title:
        return None

    addresses = hit.get("workplace_addresses") or hit.get("workplace_address") or []
    if isinstance(addresses, dict):
        addresses = [addresses]
    first = addresses[0] if addresses and isinstance(addresses[0], dict) else {}

    links = [
        link.get("url")
        for link in hit.get("source_links") or []
        if isinstance(link, dict) and link.get("url")
    ]
    url = links[0] if links else hit.get("url")

    published = parse_datetime(hit.get("publication_date")) or parse_datetime(
        hit.get("firstSeen") or hit.get("first_seen")
    )

    return Job(
        id=make_job_id("jobtech_links", ad_id),
        title=title.strip(),
        company=dig(hit, "employer", "name"),
        company_org_nr=dig(hit, "employer", "organization_number"),
        location=first.get("municipality") or first.get("city"),
        region=first.get("region"),
        description=hit.get("brief") or dig(hit, "description", "text") or "",
        url=url,
        apply_url=url,
        published_at=published,
        deadline=parse_datetime(hit.get("application_deadline")),
        sources=[SourceRef(source="jobtech_links", source_id=ad_id, url=url)],
    )
