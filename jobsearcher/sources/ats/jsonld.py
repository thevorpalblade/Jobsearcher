"""Generic careers pages: job ads that embed schema.org `JobPosting` JSON-LD (which
Google for Jobs reads, so many sites have it, e.g. Randstad). `ref` is the careers
listing page; links on it that look like job ads are fetched (capped) and their
JobPosting data read. Used when detection finds no supported ATS.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import urljoin, urlsplit

from jobsearcher.companies.config import Company
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.sources.ats.common import (
    AtsClient,
    Wanted,
    feed_source,
    html_to_text,
    is_sweden,
    swedish_city,
    swedish_name,
)
from jobsearcher.sources.base import parse_datetime

MAX_JOB_PAGES = 80  # per company and run: each one is a page fetch
# Give up on a site after this many job-looking pages in a row without JobPosting data.
MAX_MISSES = 10
# Ids in a URL (numbers, UUIDs) mark an individual ad rather than a category page.
_AD_ID = re.compile(r"\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}", re.I)
_LD_JSON = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I
)
_HREF = re.compile(r"""href=["']([^"'#]+)["']""", re.I)
# Path words that mark a job ad (Swedish and English).
_JOB_PATH = re.compile(
    r"/(jobb|job|jobs|lediga-jobb|lediga-tjanster|ledigt-jobb|tjanst|vacanc(y|ies)|"
    r"position|positions|careers?/[^/]+|karriar/[^/]+|annons)/",
    re.I,
)


def job_postings(html_text: str) -> list[dict[str, Any]]:
    """Every schema.org JobPosting object in a page's JSON-LD blocks."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kind = node.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                found.append(node)
            for key in ("@graph", "itemListElement", "item", "mainEntity"):
                if key in node:
                    walk(node[key])

    for block in _LD_JSON.findall(html_text):
        try:
            walk(json.loads(block.strip()))
        except json.JSONDecodeError:
            continue
    return found


def job_links(html_text: str, page_url: str) -> list[str]:
    """Same-site links on a listing page that look like individual job ads, most
    ad-like first (an id in the URL beats a bare category or filter page)."""
    page = urlsplit(page_url)
    site = page.netloc.removeprefix("www.")
    listing_path = page.path.rstrip("/")
    links: dict[str, None] = {}
    for href in _HREF.findall(html_text):
        url = urljoin(page_url, href.strip())
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.query.startswith(("pg=", "page=")):
            continue
        if parts.netloc.removeprefix("www.") != site:
            continue
        path = parts.path.rstrip("/")
        deeper = listing_path and path.startswith(listing_path + "/")
        if (deeper or _JOB_PATH.search(parts.path + "/")) and path != listing_path:
            links[url.split("#")[0]] = None

    def score(url: str) -> int:
        last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        return 2 * bool(_AD_ID.search(last)) + (len(last) > 25)

    ranked = sorted(links, key=score, reverse=True)  # stable: page order within a score
    if ranked and score(ranked[0]) > 0:
        ranked = [url for url in ranked if score(url) > 0]  # skip category pages
    return ranked


def fetch_jobs(client: AtsClient, ref: str, company: Company, wanted: Wanted) -> Iterator[Job]:
    listing = client.get_text(ref)
    source = feed_source("jsonld", ref)
    postings = job_postings(listing)  # some listing pages embed every ad
    for posting in postings:
        job = parse_posting(posting, ref, company, source)
        if job is not None and wanted(job):
            yield job
    if postings:
        return
    misses = 0
    for url in job_links(listing, ref)[:MAX_JOB_PAGES]:
        found = job_postings(client.get_text(url))[:1]
        misses = 0 if found else misses + 1
        if misses >= MAX_MISSES:
            return  # this site's ads don't carry JobPosting data
        for posting in found:
            job = parse_posting(posting, url, company, source)
            if job is not None and wanted(job):
                yield job


def _address(posting: dict[str, Any]) -> dict[str, Any]:
    places = posting.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    addresses = [p.get("address") or {} for p in places if isinstance(p, dict)]
    addresses = [a if isinstance(a, dict) else {"addressLocality": str(a)} for a in addresses]
    swedish = [a for a in addresses if is_sweden(_country(a), a.get("addressLocality"))]
    return (swedish or addresses or [{}])[0]


def _country(address: dict[str, Any]) -> str | None:
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name") or country.get("@id")
    return str(country) if country else None


def parse_posting(posting: dict[str, Any], url: str, company: Company, source: str) -> Job | None:
    title = " ".join(str(posting.get("title") or "").split())
    if not title:
        return None
    address = _address(posting)
    if is_sweden(_country(address)) is False:
        return None  # only offices abroad
    identifier = posting.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value")
    source_id = str(identifier or url)
    locality = address.get("addressLocality")
    location = swedish_city(locality) or (swedish_name(locality) if locality else None)
    employment = posting.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(str(e) for e in employment)
    hiring = posting.get("hiringOrganization")
    employer = hiring.get("name") if isinstance(hiring, dict) else None
    return Job(
        id=make_job_id(source, source_id),
        title=title,
        # A recruiter's ad may name the real employer; otherwise it's the company.
        company=employer or company.name,
        company_org_nr=company.org_nr if not employer else None,
        location=location or company.location,
        region=address.get("addressRegion"),
        remote=posting.get("jobLocationType") == "TELECOMMUTE" or None,
        description=html_to_text(posting.get("description")),
        employment_type=str(employment).replace("_", " ").lower() if employment else None,
        url=url,
        apply_url=url,
        published_at=parse_datetime(posting.get("datePosted")),
        deadline=parse_datetime(posting.get("validThrough")),
        sources=[SourceRef(source=source, source_id=source_id, url=url)],
    )
