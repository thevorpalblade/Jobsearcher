"""environmentjob.co.uk (formerly environmentjobs.co.uk): UK environmental and
conservation jobs.

It has no search API, but publishes a sitemap of every current job, and each job page
carries schema.org JobPosting data, so the adapter reads the sitemap and every job
page once per run (one request a second, as its robots.txt asks), then answers each
keyword from those. Its terms have no rule against automated access; its robots.txt
only disallows search-result paging and account pages, which this doesn't touch.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import datetime

import httpx

from jobsearcher.companies.config import Company
from jobsearcher.companies.http import PoliteClient
from jobsearcher.models import Job
from jobsearcher.sources.ats.jsonld import job_postings, parse_posting

log = logging.getLogger(__name__)

SITEMAP = "https://www.environmentjob.co.uk/jobs-sitemap.xml"
NAME = "environmentjob"
MAX_JOBS = 500  # a safety cap on pages fetched per run (the site lists about 180)
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_BOARD = Company(name="Environment Job")  # the fallback employer (every ad names its own)


class EnvironmentJobSource:
    name = NAME

    def __init__(self, client: PoliteClient):
        self.client = client
        self._jobs: list[Job] | None = None

    def all_jobs(self) -> list[Job]:
        """Every current job on the board (fetched once per run)."""
        if self._jobs is None:
            urls = _LOC.findall(self.client.get_text(SITEMAP))
            urls = [u for u in urls if "/jobs/" in u and re.search(r"/jobs/\d", u)][:MAX_JOBS]
            jobs = []
            for url in urls:
                try:
                    page = self.client.get_text(url)
                except httpx.HTTPError as exc:  # one bad page doesn't stop the rest
                    log.info("%s: %s failed: %s", NAME, url, exc)
                    continue
                for posting in job_postings(page)[:1]:
                    job = parse_posting(posting, url, _BOARD, NAME, abroad=True)
                    if job is not None:
                        jobs.append(job)
            self._jobs = jobs
        return self._jobs

    def search(self, keyword: str, published_after: datetime | None = None) -> Iterator[Job]:
        """The jobs mentioning `keyword` as a whole word or phrase (case-insensitive)."""
        pattern = re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.I)
        for job in self.all_jobs():
            if pattern.search(job.title) or pattern.search(job.description):
                yield job
