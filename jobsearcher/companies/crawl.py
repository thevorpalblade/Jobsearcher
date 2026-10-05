"""Crawl the target companies' own job feeds as one more job source."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from jobsearcher.companies.config import Company
from jobsearcher.companies.detect import detect
from jobsearcher.companies.http import PoliteClient
from jobsearcher.config import CompaniesSettings
from jobsearcher.models import Job
from jobsearcher.sources.ats import FETCHERS
from jobsearcher.sources.ats.common import feed_source
from jobsearcher.store import Store

log = logging.getLogger(__name__)


@dataclass
class CompanyFeed:
    company: Company
    ats_type: str | None
    ats_ref: str | None
    error: str | None = None

    @property
    def source(self) -> str:
        return feed_source(self.ats_type or "", self.ats_ref or "")

    @property
    def supported(self) -> bool:
        return self.ats_type in FETCHERS and bool(self.ats_ref)


@dataclass
class CompanyCrawlReport:
    companies: int = 0
    detected: int = 0  # (re-)detected this run
    with_feed: int = 0  # companies on a supported ATS
    jobs: int = 0
    failed: list[str] = field(default_factory=list)  # sources whose feed failed


def resolve_feeds(
    companies: list[Company],
    store: Store,
    client: PoliteClient,
    settings: CompaniesSettings,
    now: datetime,
    force: bool = False,
    retry_failed: bool = False,
) -> tuple[list[CompanyFeed], int]:
    """Each company's ATS, from the cached detection unless it's stale (or `force`, or
    `retry_failed` and nothing was found last time). Returns the feeds and how many
    companies were (re-)detected."""
    cached = store.company_ats()
    cutoff = now - timedelta(days=settings.redetect_after_days)
    feeds: list[CompanyFeed] = []
    detected = 0
    for company in companies:
        row = cached.get(company.slug)
        if company.ats is not None:
            feeds.append(CompanyFeed(company, company.ats.type, company.ats.ref))
            continue
        retry = retry_failed and row is not None and row["ats_type"] is None
        fresh = row is not None and datetime.fromisoformat(row["checked_at"]) >= cutoff
        if fresh and not force and not retry:
            feeds.append(CompanyFeed(company, row["ats_type"], row["ats_ref"], row["error"]))
            continue
        result = detect(company, client)
        detected += 1
        store.save_company_ats(
            company.slug, result.ats_type, result.ats_ref, result.careers_url, result.error, now
        )
        log.info(
            "%s: %s",
            company.name,
            f"{result.ats_type} {result.ats_ref}" if result.ats_type else result.error,
        )
        feeds.append(CompanyFeed(company, result.ats_type, result.ats_ref, result.error))
    return feeds, detected


def crawl_feeds(
    feeds: list[CompanyFeed],
    client: PoliteClient,
    wanted: Callable[[Job], bool],
    report: CompanyCrawlReport,
) -> Iterator[Job]:
    """Jobs from every company on a supported ATS. A failing feed is logged and
    recorded in `report.failed`, and the crawl moves on."""
    for feed in feeds:
        if not feed.supported:
            continue
        report.with_feed += 1
        fetch = FETCHERS[feed.ats_type]  # type: ignore[index]
        try:
            for job in fetch(client, feed.ats_ref, feed.company, wanted):  # type: ignore[arg-type]
                report.jobs += 1
                yield job
        except (httpx.HTTPError, ValueError) as exc:  # network/HTTP errors, bad JSON/XML
            log.warning("%s (%s) failed: %s", feed.company.name, feed.source, exc)
            report.failed.append(feed.source)
