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
from jobsearcher.places import county_of
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
    # Sources without a county (company feeds, LinkedIn) get it from the city, so
    # "Stockholm" matches Södertälje for them too, as it does for Platsbanken.
    county = job.region or county_of(job.location) or ""
    place = f"{job.location or ''} {county}".lower()
    return any(loc.lower() in place for loc in search.locations)


def search_keywords(config: Config) -> list[str]:
    """config.search.keywords plus, if enabled, every target role name/alias."""
    keywords = list(config.search.keywords)
    from jobsearcher.ranking.config import load_ranking_config, unique_casefold

    if config.search.use_target_roles:
        keywords += load_ranking_config(config.ranking_config).role_terms
    return unique_casefold(keywords)


def all_profiles(config: Config) -> list[Config]:
    """The config of every profile (one candidate each)."""
    return [config.for_profile(slug) for slug in config.profile_slugs()]


def profiles_keywords(profiles: list[Config]) -> list[str]:
    """Every profile's search keywords: the job pool is fetched once for all of them."""
    from jobsearcher.ranking.config import unique_casefold

    return unique_casefold([k for p in profiles for k in search_keywords(p)])


def profiles_companies(profiles: list[Config]) -> list[Company]:
    """Every profile's target companies, each once (by slug, first profile wins)."""
    seen: dict[str, Company] = {}
    for p in profiles:
        if p.companies_config.is_file():
            for company in load_companies(p.companies_config):
                seen.setdefault(company.slug, company)
    return list(seen.values())


def run_search(
    config: Config,
    store: Store,
    sources: list[SourceAdapter] | None = None,
    keywords: list[str] | None = None,
    companies: list[Company] | None = None,
    company_client: PoliteClient | None = None,
    profiles: list[Config] | None = None,
) -> SearchReport:
    """Fetch every currently open ad matching the keywords from each source, then
    every open job on the target companies' own feeds. One pass serves every profile:
    a job is kept if it is in any profile's region (`profiles`, default: `config`
    alone); each profile's own filters pick its jobs later, at ranking."""
    profiles = profiles or [config]
    sources = enabled_sources(config) if sources is None else sources
    keywords = profiles_keywords(profiles) if keywords is None else keywords
    if companies is None:
        companies = profiles_companies(profiles) if config.sources.companies else []
    report = SearchReport()
    started = datetime.now(UTC)

    # A profile with nothing to search for yet (no keywords, no target roles) doesn't
    # widen the filter: its empty region would otherwise mean "all of Sweden".
    searching = [p for p in profiles if search_keywords(p)] or profiles

    def wanted(job: Job) -> bool:
        return any(matches_filters(job, p.search) for p in searching)

    def keep(job: Job) -> bool:
        report.fetched += 1
        if not wanted(job):
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
        # wanted only, not keep(): this is a cheap pre-check before an adapter
        # fetches a full ad, and the job is counted when it's yielded.
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
