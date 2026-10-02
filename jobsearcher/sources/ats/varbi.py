"""Varbi (common in the Swedish public sector and universities): an RSS feed per
customer at `https://<ref>.varbi.com/en/what:rssfeed/`. The feed has the full ad
text but no location, so the company's `location` is used."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from email.utils import parsedate_to_datetime

from jobsearcher.companies.config import Company
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.sources.ats.common import AtsClient, Wanted, feed_source, html_to_text

_JOB_ID = re.compile(r"jobID:(\d+)")


def feed_url(ref: str) -> str:
    return f"https://{ref}.varbi.com/en/what:rssfeed/"


def fetch_jobs(client: AtsClient, ref: str, company: Company, wanted: Wanted) -> Iterator[Job]:
    root = ET.fromstring(client.get_text(feed_url(ref)))
    for item in root.iter("item"):
        job = parse_item(item, company, feed_source("varbi", ref))
        if job is not None:
            yield job


def parse_item(item: ET.Element, company: Company, source: str) -> Job | None:
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or "").strip()
    if not title or not link:
        return None
    match = _JOB_ID.search(link)
    source_id = match.group(1) if match else link
    published = None
    if item.findtext("pubDate"):
        try:
            published = parsedate_to_datetime(item.findtext("pubDate"))
        except (TypeError, ValueError):
            published = None
    return Job(
        id=make_job_id(source, source_id),
        title=title,
        company=company.name,
        company_org_nr=company.org_nr,
        location=company.location,
        description=html_to_text(item.findtext("description")),
        url=link,
        apply_url=link,
        published_at=published,
        sources=[SourceRef(source=source, source_id=source_id, url=link)],
    )
