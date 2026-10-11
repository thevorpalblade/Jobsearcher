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
    if config.sources.arbeitsagentur.enabled and "DE" in plan.countries:
        # A quick API: all its German places in the main run, abroad or not.
        from jobsearcher.companies.http import PoliteClient
        from jobsearcher.sources.arbeitsagentur import ArbeitsagenturSource

        sources.append(
            ArbeitsagenturSource(
                PoliteClient.from_config(config), config.sources.arbeitsagentur, plan.targets
            )
        )
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
    """Slow sources (rate limits, slow APIs), searched after the rest of a run has
    been ranked so they don't hold it up: eurojobs, EURES, and LinkedIn/Indeed
    outside each profile's first country (regions.py)."""
    plan = plan or SearchPlan()
    sources: list[SourceAdapter] = []
    if config.sources.eurojobs.enabled:
        from jobsearcher.companies.http import PoliteClient
        from jobsearcher.sources.eurojobs import EurojobsSource

        countries = plan.by_country() or {"SE": []}
        sources.append(
            EurojobsSource(PoliteClient.from_config(config), config.sources.eurojobs, countries)
        )
    if config.sources.eures.enabled:
        # Slow to answer (~6 s a page). Its Swedish and German ads copy Platsbanken's
        # and the Arbeitsagentur's, so those countries are left to them when they're on.
        covered = {"SE"} if config.sources.platsbanken else set()
        covered |= {"DE"} if config.sources.arbeitsagentur.enabled else set()
        countries = {c: k for c, k in plan.by_country().items() if c not in covered}
        if countries:
            from jobsearcher.companies.http import PoliteClient
            from jobsearcher.sources.eures import EuresSource

            sources.append(
                EuresSource(
                    PoliteClient.from_config(config, browser=False), config.sources.eures, countries
                )
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
    return (
        config.sources.eurojobs.enabled
        or config.sources.eures.enabled
        or bool(config.sources.jobspy.sites and any(not t.primary for t in plan.targets))
    )


__all__ = [
    "SourceAdapter", "PlatsbankenSource", "JobTechLinksSource", "enabled_sources", "slow_sources",
]  # fmt: skip
