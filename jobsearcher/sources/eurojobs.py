"""eurojobs.com: a European job board (millions of ads, mostly Spain, Italy, the UK,
Germany and France; about 10,000 in Sweden).

Its acceptable-use terms forbid bots without written permission, which the user has
(2026-10-11). It has no API or full sitemap, and its search matches any word of the
query anywhere in the ad, so for each keyword and chosen country the adapter reads
the newest results (a few pages, back to `max_age_days`), keeps those naming the
keyword as a phrase in the title or snippet, and reads each one's page for the full
ad (no JobPosting data: the HTML is parsed). All through `PoliteClient`; search pages
only one per `search_interval_s`, as the site locks searching out after about 30 quick
ones, so this is a slow source (`sources.slow_sources`), searched after the rest.
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx

from jobsearcher.companies.http import PoliteClient
from jobsearcher.config import EurojobsConfig
from jobsearcher.contacts import extract_contacts_from_text
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.sources.ats.common import html_to_text, is_sweden, swedish_name

log = logging.getLogger(__name__)

BASE = "https://www.eurojobs.com"
NAME = "eurojobs"
_CARD = re.compile(r'<article class="ej-job-result[^"]*">(.*?)</article>', re.S)
_CARD_LINK = re.compile(r'href="([^"]+/jobs/(\d+)/[^"]*)"\s+class="ej-job-result__title">', re.S)
_NEXT = re.compile(r'<a[^>]+href="([^"]+)"[^>]*rel="next"|<a[^>]+rel="next"[^>]*href="([^"]+)"')
# On /browse/countries: each country's id, and its flag (named by ISO code).
_COUNTRY = re.compile(r'country=(\d+)" class="ej-browse-page-card">.*?/flags/([a-z]{2})\.svg', re.S)
_FIELD = re.compile(
    r'<div class="small text-muted">\s*([^<]*?)\s*</div>\s*'
    r'<div class="fw-semibold[^"]*">\s*([^<]*?)\s*</div>',
    re.S,
)
_TITLE = re.compile(r'<h1 class="ej-job-detail-title[^"]*">\s*(.*?)\s*</h1>', re.S)
_DESCRIPTION = re.compile(
    r'<h2>Job description</h2>\s*<div class="ej-job-detail-section-content">'
    r"(.*?)</div>\s*</section>",
    re.S,
)
# Cloudflare hides email addresses in pages: <a ... data-cfemail="hex">[email protected]</a>
_CF_EMAIL = re.compile(r'<a[^>]*data-cfemail="([0-9a-f]+)"[^>]*>.*?</a>', re.S | re.I)


def _cf_email(match: re.Match[str]) -> str:
    """The address: each byte after the first is XORed with the first."""
    data = bytes.fromhex(match.group(1))
    address = bytes(b ^ data[0] for b in data[1:]).decode("utf-8", "replace")
    # The ads' text runs together ("name@afry.comBusiness Development Manager"): split
    # a capitalised word off the domain, and keep the address apart from the text.
    return " " + re.sub(r"(\.[a-z]{2,})([A-Z]\w*)$", r"\1 \2", address) + " "


def _text(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def _date(value: str | None) -> datetime | None:
    """ "25.11.2026" -> a datetime (the site's dates are day.month.year)."""
    try:
        return datetime.strptime(value or "", "%d.%m.%Y").replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_countries(page: str) -> dict[str, str]:
    """{"SE": "198", ...} from /browse/countries (the search takes the site's ids)."""
    return {flag.upper(): country_id for country_id, flag in _COUNTRY.findall(page)}


def parse_results(page: str) -> list[tuple[str, str, str, datetime | None]]:
    """(url, job id, title + snippet, posted) for each result on a search page."""
    results = []
    for card in _CARD.findall(page):
        link = _CARD_LINK.search(card)
        if not link:
            continue
        posted = re.search(r"Posted:</strong>\s*([\d.]+)", card)
        results.append(
            (
                html.unescape(link.group(1)),
                link.group(2),
                _text(card),
                _date(posted and posted.group(1)),
            )
        )
    return results


def next_page(page: str) -> str | None:
    match = _NEXT.search(page)
    return html.unescape(match.group(1) or match.group(2)) if match else None


def parse_job(page: str, url: str, source_id: str) -> Job | None:
    """A Job from a job's page."""
    title = _TITLE.search(page)
    if not title or not _text(title.group(1)):
        return None
    fields: dict[str, str] = {}
    for label, value in _FIELD.findall(page):  # the first is the ad's (then company info)
        fields.setdefault(_text(label).casefold(), _text(value))
    description = _DESCRIPTION.search(page)
    text = html_to_text(_CF_EMAIL.sub(_cf_email, description.group(1))) if description else ""
    # "Doha, Qatar"; "Remote" for remote jobs. Swedish cities get their Swedish names
    # (the filter then finds their county); abroad, the country is the region.
    place = [p.strip() for p in fields.get("location", "").split(",") if p.strip()]
    country = place[-1] if len(place) > 1 else None
    city = place[0] if place else None
    remote = bool(city and city.casefold() == "remote") or None
    sweden = is_sweden(country)
    employment = fields.get("employment type")
    return Job(
        id=make_job_id(NAME, source_id),
        title=_text(title.group(1)),
        # A recruiter's ad names the real employer as "Client / Employer".
        company=fields.get("client / employer") or fields.get("company"),
        location=(swedish_name(city) if sweden else city) if city and not remote else None,
        region=None if sweden or not country else country,
        remote=remote,
        description=text,
        employment_type=employment.replace("_", " ").lower() if employment else None,
        url=url,
        apply_url=url,
        published_at=_date(fields.get("posted")),
        deadline=_date(fields.get("closing date")),
        contacts=extract_contacts_from_text(text, provenance=f"{NAME}:ad_text"),
        sources=[SourceRef(source=NAME, source_id=source_id, url=url)],
    )


class EurojobsSource:
    name = NAME

    def __init__(
        self,
        client: PoliteClient,
        config: EurojobsConfig,
        countries: dict[str, list[str]],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.config = config
        # {ISO code: the keywords wanted there} (regions.SearchPlan.by_country)
        self.countries = {c: {k.casefold() for k in words} for c, words in countries.items()}
        self._sleep, self._clock = sleep, clock
        self._last_search: float | None = None
        self._country_ids: dict[str, str] | None = None
        self._jobs: dict[str, Job | None] = {}  # job pages read this run, by id

    def country_ids(self) -> dict[str, str]:
        """{ISO code: the site's id} for the countries searched that it has."""
        if self._country_ids is None:
            known = parse_countries(self.client.get_text(f"{BASE}/browse/countries"))
            self._country_ids = {c: known[c] for c in self.countries if known.get(c)}
            if missing := sorted(set(self.countries) - set(self._country_ids)):
                log.info("%s: no jobs in %s", NAME, ", ".join(missing))
        return self._country_ids

    def search(self, keyword: str, published_after: datetime | None = None) -> Iterator[Job]:
        """The newest jobs naming `keyword` as a whole word or phrase (case-insensitive),
        in the countries where it's wanted."""
        pattern = re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.I)
        oldest = datetime.now(UTC) - timedelta(days=self.config.max_age_days)
        if published_after and published_after > oldest:
            oldest = published_after
        seen: set[str] = set()  # an ad can be on two pages as new ones arrive
        for country, country_id in self.country_ids().items():
            if keyword.casefold() not in self.countries[country]:
                continue
            url: str | None = f"{BASE}/jobs"
            params: dict | None = {
                "q": keyword,
                "country": country_id,
                "sort": "newest",
                "per_page": 50,
            }
            for _ in range(self.config.max_pages):
                if url is None:
                    break
                # A failure (e.g. 429 after backing off) fails the source for this run.
                page = self._search_page(url, params)
                results = parse_results(page)
                for job_url, job_id, card_text, posted in results:
                    # Only ads naming the keyword in the title or snippet are opened:
                    # the site's matches on any one word would be thousands of pages.
                    if (
                        job_id in seen
                        or (posted and posted < oldest)
                        or not pattern.search(card_text)
                    ):
                        continue
                    seen.add(job_id)
                    job = self._job(job_url, job_id)
                    if job and (pattern.search(job.title) or pattern.search(job.description)):
                        yield job
                # Newest first: once a page ends with old ads, the rest are older.
                if not results or (results[-1][3] and results[-1][3] < oldest):
                    break
                url, params = next_page(page), None

    def _search_page(self, url: str, params: dict | None) -> str:
        """A search page, at most one per `search_interval_s` (the site's limit)."""
        if self._last_search is not None:
            wait = self.config.search_interval_s - (self._clock() - self._last_search)
            if wait > 0:
                self._sleep(wait)
        try:
            return self.client.get_text(url, params)
        finally:
            self._last_search = self._clock()

    def _job(self, url: str, job_id: str) -> Job | None:
        if job_id not in self._jobs:
            try:
                self._jobs[job_id] = parse_job(self.client.get_text(url), url, job_id)
            except httpx.HTTPError as exc:  # one bad page doesn't stop the rest
                log.info("%s: %s failed: %s", NAME, url, exc)
                self._jobs[job_id] = None
        return self._jobs[job_id]
