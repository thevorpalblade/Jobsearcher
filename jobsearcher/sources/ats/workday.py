"""Workday career sites (`<tenant>.<wdN>.myworkdayjobs.com/<site>`), via the JSON API
their own pages use: `POST /wday/cxs/<tenant>/<site>/jobs` lists jobs, `GET
/wday/cxs/<tenant>/<site><externalPath>` returns one job. The API wants a browser-like
request (`Origin`/`Referer` headers; `crawl.user_agent: chrome`).

Large employers list thousands of jobs worldwide, so the list is filtered to Sweden
with whichever facet (its name varies per employer) offers a "Sweden" value.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from jobsearcher.companies.config import Company
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.places import county_of
from jobsearcher.sources.ats.common import (
    AtsClient,
    Wanted,
    feed_source,
    html_to_text,
    swedish_city,
    swedish_name,
)
from jobsearcher.sources.base import parse_datetime

PAGE_SIZE = 20  # the API's maximum
MAX_JOBS = 500
# Postings listed in several places ("2 Locations") only say where in their details,
# so those are fetched before the location filter can run; at most this many.
MAX_UNKNOWN_LOCATION = 60
_LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")


def parse_ref(ref: str) -> tuple[str, str, str]:
    """(host, tenant, site) from a detected ref such as
    "essity.wd3.myworkdayjobs.com/en-US/Job_opportunities" (paths after the site,
    like "/login" or "/job/...", are ignored)."""
    ref = ref.split("://")[-1]
    host, _, path = ref.partition("/")
    segments = [s for s in path.split("/") if s and not _LOCALE.match(s)]
    if not host.endswith(".myworkdayjobs.com") or not segments:
        raise ValueError(f"Not a Workday career site: {ref!r}")
    return host, host.split(".")[0], segments[0]


def _headers(host: str, site: str) -> dict[str, str]:
    return {"Origin": f"https://{host}", "Referer": f"https://{host}/{site}"}


def sweden_facet(facets: list[dict[str, Any]]) -> tuple[str, str] | None:
    """(facet parameter, value id) of the country-level "Sweden" filter, if any."""
    for facet in facets or []:
        # Some employers nest groups (Saab: locationMainGroup > locationCountry); a
        # nested group is filtered by its own parameter name.
        candidates = []
        for value in facet.get("values") or []:
            if value.get("values"):
                inner = value.get("facetParameter") or facet["facetParameter"]
                candidates += [(inner, v) for v in value["values"]]
            else:
                candidates.append((facet["facetParameter"], value))
        for parameter, value in candidates:
            if str(value.get("descriptor", "")).strip().casefold() == "sweden":
                return parameter, value["id"]
    return None


def city_from(text: str | None) -> str | None:
    """ "Sweden, Stockholm" -> "Stockholm"; "Stockholm - Solna" -> "Stockholm"."""
    if not text or re.match(r"^\d+ Locations?$", text):
        return None
    parts = [p.strip() for p in text.split(",")]
    if len(parts) > 1 and parts[0].casefold() in ("sweden", "sverige"):
        return swedish_name(parts[1])
    return swedish_city(text) or swedish_name(parts[0].split(" - ")[0])


def fetch_jobs(client: AtsClient, ref: str, company: Company, wanted: Wanted) -> Iterator[Job]:
    host, tenant, site = parse_ref(ref)
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    headers = _headers(host, site)
    source = feed_source("workday", f"{host}/{site}")

    first = client.post_json(
        f"{api}/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}, headers
    )
    facet = sweden_facet(first.get("facets") or [])
    # No country filter: usually a site for Sweden only (e.g. Apotek Hjärtat); list
    # everything and let the location filter decide.
    applied = {facet[0]: [facet[1]]} if facet else {}
    offset, total, unknown = 0, None, 0
    while offset < min(total if total is not None else MAX_JOBS, MAX_JOBS):
        page = client.post_json(
            f"{api}/jobs",
            {"appliedFacets": applied, "limit": PAGE_SIZE, "offset": offset, "searchText": ""},
            headers,
        )
        postings = page.get("jobPostings") or []
        total = page.get("total", 0) if total is None else total
        for posting in postings:
            job = parse_posting(posting, company, source, host, site)
            if job is None:
                continue
            located = job.location is not None
            if located and not wanted(job):
                continue
            if not located:
                if unknown >= MAX_UNKNOWN_LOCATION:
                    continue
                unknown += 1
            detail: Any = client.get_json(f"{api}{posting['externalPath']}", None, headers)
            job = with_details(job, detail)
            if located or wanted(job):
                yield job
        offset += len(postings)
        if not postings:
            return


def parse_posting(
    posting: dict[str, Any], company: Company, source: str, host: str, site: str
) -> Job | None:
    title, path = posting.get("title"), posting.get("externalPath")
    if not title or not path:
        return None
    bullets = posting.get("bulletFields") or []
    source_id = str(bullets[0] if bullets else path)
    url = f"https://{host}/{site}{path}"
    return Job(
        id=make_job_id(source, source_id),
        title=title.strip(),
        company=company.name,
        company_org_nr=company.org_nr,
        # "2 Locations" says nothing; the detail page has the main one.
        location=city_from(posting.get("locationsText")) or company.location,
        url=url,
        apply_url=url,
        sources=[SourceRef(source=source, source_id=source_id, url=url)],
    )


def best_location(info: dict[str, Any], fallback: str | None) -> str | None:
    """The city to file a posting under. A posting can list several places ("6 Locations":
    a main one, often abroad, plus additional ones); the main place wins unless an
    additional one is somewhere we recognise as Swedish, which wins instead, so a job
    based in Germany that is also open in Sundbyberg isn't dropped by the location filter."""
    main = city_from(info.get("location")) or fallback
    if main and (swedish_city(main) or county_of(main)):
        return main
    extra = info.get("additionalLocations") or []
    if isinstance(extra, str):
        extra = [extra]
    for text in extra:
        city = city_from(text)
        if city and (swedish_city(text) or county_of(city)):
            return city
    return main


def with_details(job: Job, detail: dict[str, Any]) -> Job:
    info = detail.get("jobPostingInfo") or {}
    url = info.get("externalUrl") or job.url
    update: dict[str, Any] = {
        "description": html_to_text(info.get("jobDescription")),
        "employment_type": info.get("timeType"),
        "published_at": parse_datetime(info.get("startDate")),
        "deadline": parse_datetime(info.get("endDate")),
        "url": url,
        "apply_url": url,
        "location": best_location(info, job.location),
    }
    job = job.model_copy(update=update)
    job.sources[0].url = url
    return job
