"""Pipeline stages. M1 implements the search stage; ranking/drafting follow."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

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
) -> SearchReport:
    """Fetch every currently open ad matching the keywords from each source."""
    sources = enabled_sources(config) if sources is None else sources
    keywords = search_keywords(config) if keywords is None else keywords
    report = SearchReport()
    started = datetime.now(UTC)

    for source in sources:
        seen_ids: set[str] = set()
        try:
            for keyword in keywords:
                for job in source.search(keyword, published_after=None):
                    if job.id in seen_ids:
                        continue  # same ad matched several keywords
                    seen_ids.add(job.id)
                    report.fetched += 1
                    if not matches_filters(job, config.search):
                        report.filtered_out += 1
                        continue
                    _, created = store.upsert_job(job, now=started)
                    if created:
                        report.new += 1
                    else:
                        report.updated += 1
        except Exception:
            log.exception("Source %s failed", source.name)
            report.failed_sources.append(source.name)
            continue
        store.set_last_run(source.name, started)
        log.info("%s: %d ads fetched", source.name, len(seen_ids))

    # If a source failed, its jobs weren't re-seen; don't expire them because of that.
    if not report.failed_sources:
        report.expired = store.expire_jobs(config.search.expire_after_days, now=started)
    return report
