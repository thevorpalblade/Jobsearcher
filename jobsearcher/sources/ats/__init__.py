"""Adapters for applicant-tracking systems (ATS) that publish open job feeds."""

from jobsearcher.sources.ats import greenhouse, lever, smartrecruiters, teamtailor, varbi
from jobsearcher.sources.ats.common import AtsFetcher

FETCHERS: dict[str, AtsFetcher] = {
    "teamtailor": teamtailor.fetch_jobs,
    "varbi": varbi.fetch_jobs,
    "lever": lever.fetch_jobs,
    "greenhouse": greenhouse.fetch_jobs,
    "smartrecruiters": smartrecruiters.fetch_jobs,
}

__all__ = ["FETCHERS"]
