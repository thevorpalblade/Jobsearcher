"""EURES, the European Commission's job portal: ads from the public employment services
of the EU/EEA countries and Switzerland (e.g. Ireland's Intreo, Switzerland's job-room).

It has a public search API, the one its website uses (no login). Its search matches
any word of the query, so for each keyword and country the adapter reads the newest
ads (a few pages of 50, back to `max_age_days`) and keeps those naming the keyword as
a phrase in the title or text. Each ad comes with its full text, so one request per
page. Places are EU statistical regions (NUTS: "IE061"), named from places.nuts_name.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from jobsearcher.companies.http import PoliteClient
from jobsearcher.config import EuresConfig
from jobsearcher.contacts import extract_contacts_from_text
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.places import country_name, nuts_name
from jobsearcher.sources.ats.common import html_to_text

log = logging.getLogger(__name__)

NAME = "eures"
SEARCH = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
AD_URL = "https://europa.eu/eures/portal/jv-se/jv-details/{id}?lang=en"
PAGE_SIZE = 50  # the API's most
# EURES's country codes are ISO's in lower case, except Greece's.
_CODES = {"GR": "el"}
# Its texts escape Markdown ("High\-Tech").
_ESCAPED = re.compile(r"\\([-&*_#.()\[\]!+])")


def _body(keyword: str, country: str, page: int) -> dict[str, Any]:
    empty: list[Any] = []
    return {
        "resultsPerPage": PAGE_SIZE, "page": page, "sortSearch": "MOST_RECENT",
        "keywords": [{"keyword": keyword, "specificSearchCode": "EVERYWHERE"}],
        "locationCodes": [_CODES.get(country, country.lower())],
        "publicationPeriod": None, "occupationUris": empty, "skillUris": empty,
        "requiredExperienceCodes": empty, "positionScheduleCodes": empty,
        "sectorCodes": empty, "educationAndQualificationLevelCodes": empty,
        "positionOfferingCodes": empty, "euresFlagCodes": empty,
        "otherBenefitsCodes": empty, "requiredLanguages": empty, "minNumberPost": None,
    }  # fmt: skip


def _posted(ad: dict[str, Any]) -> datetime | None:
    millis = ad.get("creationDate")
    return datetime.fromtimestamp(millis / 1000, tz=UTC) if millis else None


def parse_ad(ad: dict[str, Any]) -> Job | None:
    """A Job from one search result."""
    ad_id, title = ad.get("id"), " ".join(str(ad.get("title") or "").split())
    if not ad_id or not title:
        return None
    # {"IE": ["IE061"]}: the country, and its region (NUTS 3, the finest) if given.
    country, regions = next(iter((ad.get("locationMap") or {}).items()), (None, []))
    code = next((r for r in regions or [] if r), None)
    area = nuts_name(code[:4]) if code and len(code) > 4 else None  # its NUTS 2 region
    employer = (ad.get("employer") or {}).get("name")
    text = html_to_text(_ESCAPED.sub(r"\1", str(ad.get("description") or "")))
    schedule = ad.get("positionScheduleCodes") or []
    url = AD_URL.format(id=ad_id)
    return Job(
        id=make_job_id(NAME, ad_id),
        title=title,
        company=None if not employer or employer.casefold() == "confidential" else employer,
        location=nuts_name(code),  # "Dublin", "Roma", "München, Kreisfreie Stadt"
        region=", ".join(x for x in (area, country_name(country) if country else None) if x)
        or None,
        description=text,
        employment_type={"fulltime": "full time", "parttime": "part time"}.get(
            schedule[0] if schedule else ""
        ),
        url=url,
        apply_url=url,
        published_at=_posted(ad),
        contacts=extract_contacts_from_text(text, provenance=f"{NAME}:ad_text"),
        sources=[SourceRef(source=NAME, source_id=ad_id, url=url)],
    )


class EuresSource:
    name = NAME

    def __init__(self, client: PoliteClient, config: EuresConfig, countries: dict[str, list[str]]):
        self.client = client
        self.config = config
        # {ISO code: the keywords wanted there} (regions.SearchPlan.by_country)
        self.countries = {c: {k.casefold() for k in words} for c, words in countries.items()}

    def search(self, keyword: str, published_after: datetime | None = None) -> Iterator[Job]:
        """The newest ads naming `keyword` as a whole word or phrase (case-insensitive),
        in the countries where it's wanted."""
        pattern = re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.I)
        oldest = datetime.now(UTC) - timedelta(days=self.config.max_age_days)
        seen: set[str] = set()
        for country, wanted in self.countries.items():
            if keyword.casefold() not in wanted:
                continue
            for page in range(1, self.config.max_pages + 1):
                # A failure fails the source for this run (its jobs then aren't expired).
                data = self.client.post_json(SEARCH, _body(keyword, country, page))
                ads = data.get("jvs") or []
                for ad in ads:
                    posted = _posted(ad)
                    if ad.get("id") in seen or (posted and posted < oldest):
                        continue
                    seen.add(ad.get("id"))
                    job = parse_ad(ad)
                    if job and (pattern.search(job.title) or pattern.search(job.description)):
                        yield job
                # Newest first: once a page ends with old ads, the rest are older.
                last = _posted(ads[-1]) if ads else None
                if len(ads) < PAGE_SIZE or (last and last < oldest):
                    break
