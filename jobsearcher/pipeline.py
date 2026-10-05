"""The search stage: job boards searched by keyword, then the target companies' own
job feeds. Ranking lives in `jobsearcher.ranking`."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from jobsearcher.companies.config import Company, load_companies
from jobsearcher.companies.crawl import CompanyCrawlReport, crawl_feeds, resolve_feeds
from jobsearcher.companies.http import PoliteClient
from jobsearcher.config import Config, SearchConfig
from jobsearcher.models import Job
from jobsearcher.sources import SourceAdapter, enabled_sources
from jobsearcher.store import Store

log = logging.getLogger(__name__)


@dataclass
class SearchReport:
    fetched: int = 0
    filtered_out: int = 0
    new: int = 0
    updated: int = 0
    expired: int = 0
    failed_sources: list[str] = field(default_factory=list)
    companies: CompanyCrawlReport | None = None


def matches_filters(job: Job, search: SearchConfig) -> bool:
    haystack = f"{job.title}\n{job.description}".lower()
    if any(term.lower() in haystack for term in search.exclude_keywords):
        return False
    if not search.locations:
        return True
    if search.include_remote and job.remote:
        return True
    place = f"{job.location or ''} {job.region or ''}".lower()
    return any(loc.lower() in place for loc in search.locations)


def search_keywords(config: Config) -> list[str]:
    """config.search.keywords plus, if enabled, every target role name/alias."""
    keywords = list(config.search.keywords)
    from jobsearcher.ranking.config import load_ranking_config, unique_casefold

    if config.search.use_target_roles:
        keywords += load_ranking_config(config.ranking_config).role_terms
    return unique_casefold(keywords)


def run_search(
    config: Config,
    store: Store,
    sources: list[SourceAdapter] | None = None,
    keywords: list[str] | None = None,
    companies: list[Company] | None = None,
    company_client: PoliteClient | None = None,
) -> SearchReport:
    """Fetch every currently open ad matching the keywords from each source, then
    every open job on the target companies' own feeds."""
    sources = enabled_sources(config) if sources is None else sources
    keywords = search_keywords(config) if keywords is None else keywords
    if companies is None:
        companies = load_companies(config.companies_config) if config.sources.companies else []
    report = SearchReport()
    started = datetime.now(UTC)

    def keep(job: Job) -> bool:
        report.fetched += 1
        if not matches_filters(job, config.search):
            report.filtered_out += 1
            return False
        return True

    def save(job: Job) -> None:
        _, created = store.upsert_job(job, now=started)
        if created:
            report.new += 1
        else:
            report.updated += 1

    for source in sources:
        seen_ids: set[str] = set()
        try:
            for keyword in keywords:
                for job in source.search(keyword, published_after=None):
                    if job.id in seen_ids:
                        continue  # same ad matched several keywords
                    seen_ids.add(job.id)
                    if keep(job):
                        save(job)
        except Exception:
            log.exception("Source %s failed", source.name)
            report.failed_sources.append(source.name)
            continue
        store.set_last_run(source.name, started)
        log.info("%s: %d ads fetched", source.name, len(seen_ids))

    skip = set(report.failed_sources)
    if companies:
        report.companies = crawl = CompanyCrawlReport(companies=len(companies))
        client = company_client or PoliteClient.from_config(config)
        feeds, crawl.detected = resolve_feeds(companies, store, client, config.companies, started)
        # matches_filters only, not keep(): this is a cheap pre-check before an
        # adapter fetches a full ad, and the job is counted when it's yielded.
        wanted = lambda job: matches_filters(job, config.search)  # noqa: E731
        for job in crawl_feeds(feeds, client, wanted, crawl):
            if keep(job):
                save(job)
        skip.update(crawl.failed)

    # A failed source's jobs weren't re-seen this run; don't expire them because of it.
    # JobSpy sites only return recent ads, so their jobs expire by age instead.
    by_age = set(config.sources.jobspy.sites)
    report.expired = store.expire_jobs(
        config.search.expire_after_days, now=started, skip_sources=skip | by_age
    )
    if by_age:
        report.expired += store.expire_by_age(
            by_age, config.sources.jobspy.max_age_days, now=started
        )
    return report
