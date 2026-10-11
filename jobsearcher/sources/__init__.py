import logging

from jobsearcher.config import Config
from jobsearcher.regions import SearchPlan
from jobsearcher.sources.base import SourceAdapter, make_client
from jobsearcher.sources.jobtech_links import JobTechLinksSource
from jobsearcher.sources.platsbanken import PlatsbankenSource

log = logging.getLogger(__name__)


def enabled_sources(config: Config, plan: SearchPlan | None = None) -> list[SourceAdapter]:
    """The sources of the main search: those switched on whose country is in the plan
    (regions.py; no plan: all of Sweden), LinkedIn/Indeed in each profile's first country."""
    plan = plan or SearchPlan()
    sources: list[SourceAdapter] = []

    def client():  # one per source: they're used one at a time, but closed separately
        return make_client(user_agent=config.crawl.user_agent_string)

    if config.sources.platsbanken and "SE" in plan.countries:
        sources.append(PlatsbankenSource(client()))
    if config.sources.jobtech_links and "SE" in plan.countries:
        sources.append(
            JobTechLinksSource(client(), skip_platsbanken_only=config.sources.platsbanken)
        )
    if config.sources.environmentjob and "GB" in plan.countries:
        from jobsearcher.companies.http import PoliteClient
        from jobsearcher.sources.environmentjob import EnvironmentJobSource

        sources.append(EnvironmentJobSource(PoliteClient.from_config(config)))
    jobspy = config.sources.jobspy
    if jobspy.sites:
        from jobsearcher.sources.jobspy_source import JobSpySource, available

        if available():
            targets = [t for t in plan.targets if t.primary] if plan.targets else None
            sources += [JobSpySource(site, jobspy, targets) for site in jobspy.sites]
        else:
            log.warning("sources.jobspy is set but JobSpy isn't installed: pip install '.[jobspy]'")
    return sources


def slow_sources(config: Config, plan: SearchPlan | None = None) -> list[SourceAdapter]:
    """Sources that take hours under their rate limits, searched after the rest of a
    run has been ranked so they don't hold it up: eurojobs, and LinkedIn/Indeed outside
    each profile's first country (regions.py)."""
    plan = plan or SearchPlan()
    sources: list[SourceAdapter] = []
    if config.sources.eurojobs.enabled:
        from jobsearcher.companies.http import PoliteClient
        from jobsearcher.sources.eurojobs import EurojobsSource

        countries = plan.by_country() or {"SE": []}
        sources.append(
            EurojobsSource(PoliteClient.from_config(config), config.sources.eurojobs, countries)
        )
    jobspy = config.sources.jobspy
    abroad = [t for t in plan.targets if not t.primary]
    if jobspy.sites and abroad:
        from jobsearcher.sources.jobspy_source import JobSpySource, available

        if available():
            sources += [
                JobSpySource(site, jobspy, abroad, pause_s=jobspy.slow_pause_s)
                for site in jobspy.sites
            ]
    return sources


def has_slow_sources(config: Config, plan: SearchPlan) -> bool:
    return config.sources.eurojobs.enabled or bool(
        config.sources.jobspy.sites and any(not t.primary for t in plan.targets)
    )


__all__ = [
    "SourceAdapter", "PlatsbankenSource", "JobTechLinksSource", "enabled_sources", "slow_sources",
]  # fmt: skip
