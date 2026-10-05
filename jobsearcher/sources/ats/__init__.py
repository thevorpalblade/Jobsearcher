"""Adapters for applicant-tracking systems (ATS) that publish open job feeds."""

from jobsearcher.sources.ats import (
    greenhouse,
    jobylon,
    jsonld,
    lever,
    reachmee,
    smartrecruiters,
    successfactors,
    teamtailor,
    varbi,
    workday,
)
from jobsearcher.sources.ats.common import AtsFetcher

FETCHERS: dict[str, AtsFetcher] = {
    "teamtailor": teamtailor.fetch_jobs,
    "varbi": varbi.fetch_jobs,
    "lever": lever.fetch_jobs,
    "greenhouse": greenhouse.fetch_jobs,
    "smartrecruiters": smartrecruiters.fetch_jobs,
    "workday": workday.fetch_jobs,
    "successfactors": successfactors.fetch_jobs,
    "reachmee": reachmee.fetch_jobs,
    "jobylon": jobylon.fetch_jobs,
    "jsonld": jsonld.fetch_jobs,  # generic: schema.org JobPosting on job pages
}

__all__ = ["FETCHERS"]
