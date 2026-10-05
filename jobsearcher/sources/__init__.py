from jobsearcher.config import Config
from jobsearcher.sources.base import SourceAdapter, make_client
from jobsearcher.sources.jobtech_links import JobTechLinksSource
from jobsearcher.sources.platsbanken import PlatsbankenSource


def enabled_sources(config: Config) -> list[SourceAdapter]:
    sources: list[SourceAdapter] = []

    def client():  # one per source: they're used one at a time, but closed separately
        return make_client(user_agent=config.crawl.user_agent_string)

    if config.sources.platsbanken:
        sources.append(PlatsbankenSource(client()))
    if config.sources.jobtech_links:
        sources.append(
            JobTechLinksSource(client(), skip_platsbanken_only=config.sources.platsbanken)
        )
    return sources


__all__ = ["SourceAdapter", "PlatsbankenSource", "JobTechLinksSource", "enabled_sources"]
