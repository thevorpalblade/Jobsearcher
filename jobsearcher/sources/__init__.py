from jobsearcher.config import Config
from jobsearcher.sources.base import SourceAdapter
from jobsearcher.sources.jobtech_links import JobTechLinksSource
from jobsearcher.sources.platsbanken import PlatsbankenSource


def enabled_sources(config: Config) -> list[SourceAdapter]:
    sources: list[SourceAdapter] = []
    if config.sources.platsbanken:
        sources.append(PlatsbankenSource())
    if config.sources.jobtech_links:
        sources.append(JobTechLinksSource())
    return sources


__all__ = ["SourceAdapter", "PlatsbankenSource", "JobTechLinksSource", "enabled_sources"]
