"""The signals stage: fetch news for every target company, classify it, build a digest."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from jobsearcher.companies.config import Company
from jobsearcher.signals.news import fetch_google_news, fetch_news
from jobsearcher.sources.ats.common import AtsClient
from jobsearcher.store import Store

log = logging.getLogger(__name__)

RUN_KEY = "signals"  # in the store's runs table


@dataclass
class FetchReport:
    companies: int = 0
    new_items: int = 0
    failed: list[str] = field(default_factory=list)


def fetch_all_news(
    store: Store, client: AtsClient, companies: list[Company], days: int, source: str = "gdelt"
) -> FetchReport:
    """Fetch news for every company from `source` ("gdelt" or "google_news")."""
    fetch = fetch_google_news if source == "google_news" else fetch_news
    report = FetchReport(companies=len(companies))
    for company in companies:
        try:
            items = fetch(client, company, days)
        except (httpx.HTTPError, ValueError) as exc:  # network/HTTP errors, non-JSON reply
            log.warning("News for %s failed: %s", company.name, exc)
            report.failed.append(company.name)
            continue
        report.new_items += store.save_news_items(items)
    return report


def due(store: Store, every_days: int, now: datetime | None = None) -> bool:
    last = store.last_run(RUN_KEY)
    return last is None or (now or datetime.now(UTC)) - last >= timedelta(days=every_days)


@dataclass
class CompanyDigest:
    company: Company
    score: int  # best relevance among its recent signals
    signals: list = field(default_factory=list)  # rows, most relevant first
    open_jobs: int = 0


def digest(
    store: Store, companies: list[Company], days: int, min_relevance: int = 40
) -> list[CompanyDigest]:
    """Companies with recent relevant signals, best first."""
    by_slug = {c.slug: c for c in companies}
    since = datetime.now(UTC) - timedelta(days=days)
    grouped: dict[str, list] = defaultdict(list)
    for row in store.signals_since(since):
        if row["company"] in by_slug and row["kind"] != "not_about_company":
            grouped[row["company"]].append(row)
    jobs_by_company: dict[str, int] = defaultdict(int)
    for job in store.iter_jobs():
        jobs_by_company[(job.company or "").casefold()] += 1
    out = [
        CompanyDigest(
            company=by_slug[slug],
            score=rows[0]["relevance"],
            signals=rows,
            open_jobs=jobs_by_company.get(by_slug[slug].name.casefold(), 0),
        )
        for slug, rows in grouped.items()
        if rows[0]["relevance"] >= min_relevance
    ]
    out.sort(key=lambda d: d.score, reverse=True)
    return out
