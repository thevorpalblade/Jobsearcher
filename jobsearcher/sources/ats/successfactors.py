"""SAP SuccessFactors career sites ("Recruiting Marketing", e.g. jobs.scania.com,
jobs.volvocars.com, jobb.axfood.se): their RSS feed lists every open job, with the
full ad, when asked for enough rows. `ref` is the career site's base URL.

The location is only in the title, as a trailing "(City, Region/Code, ...)", in
formats that vary per employer; jobs placed outside Sweden are dropped.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from jobsearcher.companies.config import Company
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.sources.ats.common import (
    AtsClient,
    Wanted,
    feed_source,
    html_to_text,
    swedish_city,
    swedish_name,
)

MAX_ROWS = 3000
_PLACE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
_SWEDEN = {"se", "swe", "sweden", "sverige"}
_JOB_ID = re.compile(r"/(\d{5,})/?(?:\?|$)")


def base_url(ref: str) -> str:
    parts = urlsplit(ref if "://" in ref else f"https://{ref}")
    if "successfactors." in parts.netloc:
        raise ValueError(
            f"{ref!r} is a SuccessFactors system link, not a career site; "
            "set careers_url in companies.yaml"
        )
    return f"{parts.scheme}://{parts.netloc}"


def feed_url(ref: str) -> str:
    return f"{base_url(ref)}/services/rss/job/?locale=en_US&rows={MAX_ROWS}"


def split_title(title: str) -> tuple[str, list[str]]:
    """("HR Partner (Solna, AB, 171 54)") -> ("HR Partner", ["Solna", "AB", "171 54"])."""
    match = _PLACE.match(title.strip())
    if not match:
        return title.strip(), []
    return match.group(1).strip(), [p.strip() for p in match.group(2).split(",") if p.strip()]


def place(parts: list[str]) -> tuple[str | None, bool | None]:
    """(city, in Sweden?) from the title's location parts; None when unknown."""
    if not parts:
        return None, None
    city = swedish_city(parts[0]) or swedish_name(parts[0])
    if any(p.casefold() in _SWEDEN for p in parts) or swedish_city(parts[0]):
        return city, True
    return city, False


def fetch_jobs(client: AtsClient, ref: str, company: Company, wanted: Wanted) -> Iterator[Job]:
    root = ET.fromstring(client.get_text(feed_url(ref)))
    source = feed_source("successfactors", base_url(ref))
    for item in root.iter("item"):
        job = parse_item(item, company, source)
        if job is not None and wanted(job):
            yield job


def parse_item(item: ET.Element, company: Company, source: str) -> Job | None:
    raw_title, link = (item.findtext("title") or "").strip(), (item.findtext("link") or "").strip()
    if not raw_title or not link:
        return None
    title, parts = split_title(raw_title)
    city, in_sweden = place(parts)
    if in_sweden is False:
        return None
    url = link.split("?")[0]  # drop the feed's tracking parameters
    match = _JOB_ID.search(url)
    source_id = match.group(1) if match else url
    published = None
    if pub := item.findtext("pubDate"):
        try:
            published = parsedate_to_datetime(pub)
        except (TypeError, ValueError):
            published = None
    return Job(
        id=make_job_id(source, source_id),
        title=title,
        company=company.name,
        company_org_nr=company.org_nr,
        location=city or company.location,
        description=html_to_text(item.findtext("description")),
        url=url,
        apply_url=url,
        published_at=published,
        sources=[SourceRef(source=source, source_id=source_id, url=url)],
    )
