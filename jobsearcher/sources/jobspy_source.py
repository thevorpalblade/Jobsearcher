"""LinkedIn and Indeed (also Glassdoor and Google Jobs) via JobSpy.

JobSpy (`pip install '.[jobspy]'`) scrapes the sites' public, logged-out search pages
while impersonating a browser. It breaks the sites' terms of service; the user runs
this at low volume for one person. A blocked or failing search is logged and the
source is skipped for that run.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from typing import Any

from jobsearcher.config import JobSpyConfig
from jobsearcher.contacts import extract_contacts_from_text
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.places import HOME, country_code, country_name
from jobsearcher.regions import SearchArea, Target
from jobsearcher.sources.ats.common import swedish_name

log = logging.getLogger(__name__)


def available() -> bool:
    try:
        import jobspy  # noqa: F401
    except ImportError:
        return False
    return True


def _value(row: dict[str, Any], key: str) -> Any:
    """A cell, with pandas' NaN for "missing" turned into None."""
    value = row.get(key)
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def split_location(text: str | None) -> tuple[str | None, str | None]:
    """("Gothenburg, Västra Götaland County, Sweden") -> ("Göteborg", "Västra Götaland");
    abroad the country stays in the region, for a region like "Ireland":
    ("Dublin, County Dublin, Ireland") -> ("Dublin", "County Dublin, Ireland")."""
    if not text:
        return None, None
    parts = [p.strip() for p in str(text).split(",") if p.strip()]
    country = country_code(parts[-1]) if parts else None
    if country and len(parts) == 1:
        return None, country_name(country)  # the whole country
    if country and country != HOME:
        return parts[0], ", ".join(parts[1:-1] + [country_name(country)])
    city = parts[0] if parts else None
    if city:
        city = swedish_name(city)
    region = parts[1].removesuffix(" County").strip() if len(parts) > 2 else None
    return city, region


def parse_row(row: dict[str, Any], site: str) -> Job | None:
    raw_id, title, url = _value(row, "id"), _value(row, "title"), _value(row, "job_url")
    if not raw_id or not title or not url:
        return None
    city, region = split_location(_value(row, "location"))
    posted = _value(row, "date_posted")
    if isinstance(posted, date) and not isinstance(posted, datetime):
        posted = datetime(posted.year, posted.month, posted.day, tzinfo=UTC)
    description = str(_value(row, "description") or "")
    source_id = str(raw_id)
    return Job(
        id=make_job_id(site, source_id),
        title=str(title).strip(),
        company=_value(row, "company"),
        company_url=_value(row, "company_url_direct"),
        location=city,
        region=region,
        remote=bool(_value(row, "is_remote")) or None,
        description=description,
        employment_type=_value(row, "job_type"),
        url=str(url),
        apply_url=_value(row, "job_url_direct") or str(url),
        published_at=posted if isinstance(posted, datetime) else None,
        contacts=extract_contacts_from_text(description, provenance=f"{site}:ad_text"),
        sources=[SourceRef(source=site, source_id=source_id, url=str(url))],
    )


def indeed_country(code: str) -> str | None:
    """JobSpy's name for a country's Indeed site ("IE" -> "ireland"); None if Indeed
    has none there."""
    try:
        from jobspy.model import Country
    except ImportError:  # tests: no JobSpy
        return country_name(code).lower()
    for country in Country:
        names, codes = country.value[0], country.value[1]
        if codes.split(":")[-1] == code.lower() and country.name not in ("US_CANADA", "WORLDWIDE"):
            return names.split(",")[0]
    return None


class JobSpySource:
    """One JobSpy site as a keyword-searchable source (name = the site), searched in
    the plan's areas (regions.py) that want the keyword; no targets: all of Sweden."""

    def __init__(
        self,
        site: str,
        settings: JobSpyConfig,
        targets: list[Target] | None = None,
        pause_s: float | None = None,
        scrape: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.name = site
        self.settings = settings
        self.targets = targets
        self.pause_s = settings.pause_s if pause_s is None else pause_s
        self._scrape = scrape
        self._sleep = sleep
        self._searched = False

    def search(self, keyword: str, published_after: datetime | None = None) -> Iterator[Job]:
        if self.targets is None:
            areas = [SearchArea(country=HOME)]
        else:
            areas = [t.area for t in self.targets if t.wants(keyword)]
        for area in areas:
            yield from self._search_area(keyword, area)

    def _search_area(self, keyword: str, area: SearchArea) -> Iterator[Job]:
        indeed = indeed_country(area.country)
        # Indeed is a site per country: in one without it, there's nothing to search.
        if indeed is None and self.name in ("indeed", "glassdoor"):
            return
        if self._scrape is None:
            from jobspy import scrape_jobs

            self._scrape = scrape_jobs
        if self._searched:
            self._sleep(self.pause_s)  # gentler on the site's rate limits
        self._searched = True
        frame = self._scrape(
            site_name=[self.name],
            search_term=keyword,
            # LinkedIn takes "Dublin, Ireland"; Indeed a place on its country's site.
            location=area.label() if self.name == "linkedin" else (area.place or area.label()),
            results_wanted=self.settings.results_per_search,
            hours_old=self.settings.hours_old,
            country_indeed=indeed or "worldwide",
            linkedin_fetch_description=True,
            description_format="markdown",
        )
        rows = frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame or [])
        for row in rows:
            job = parse_row(row, self.name)
            if job is not None:
                yield job
