"""Find where a company posts its jobs: its careers page and the ATS behind it."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from jobsearcher.companies.config import Company
from jobsearcher.companies.http import PoliteClient

log = logging.getLogger(__name__)

# (ATS type, pattern whose group 1 is the ref the adapter needs). Order matters:
# the first match wins. Types without an adapter yet are still recorded, so
# `jobsearcher companies` shows which adapters would pay off.
ATS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9._-]+)")),
    (
        "greenhouse",
        re.compile(
            r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/"
            r"(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)"
        ),
    ),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([a-z0-9-]+)")),
    ("varbi", re.compile(r"https?://([a-z0-9-]+)\.varbi\.com")),
    ("teamtailor", re.compile(r"https?://([a-z0-9-]+\.teamtailor\.com)")),
    ("workday", re.compile(r"https?://([a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/[^\s\"'<>?#]*)")),
    ("reachmee", re.compile(r"https?://([a-z0-9.-]*reachmee\.com/[^\s\"'<>?#]*)")),
    ("jobylon", re.compile(r"https?://([a-z0-9.-]*jobylon\.com/[^\s\"'<>?#]*)")),
    ("successfactors", re.compile(r"https?://([a-z0-9.-]*successfactors\.(?:com|eu)[^\s\"'<>]*)")),
]
# Shared hosts every page on that ATS links to, not a customer (e.g. Teamtailor's
# tracking script at tt.teamtailor.com).
_IGNORED_REFS = {"www", "api", "embed", "static", "assets", "cdn", "app", "jobs", "careers", "tt"}

# A careers site served by Teamtailor under the company's own domain.
_TEAMTAILOR_HOSTED = re.compile(r"teamtailor-cdn\.com|assets\.teamtailor|teamtailor\.com/assets")

# Links that probably lead to the careers page, in Swedish and English.
_CAREERS_WORDS = re.compile(
    r"karri[aä]r|career|jobb|jobs|lediga|tj[aä]nster|work[-_ ]?with[-_ ]?us|join[-_ ]?us|"
    r"jobba|arbeta[-_ ]hos|vacanc",
    re.IGNORECASE,
)
MAX_CAREERS_PAGES = 3


@dataclass
class Detection:
    ats_type: str | None
    ats_ref: str | None
    careers_url: str | None
    error: str | None = None


def find_ats(html_text: str, page_url: str) -> tuple[str, str] | None:
    """The first known ATS referenced in a page, as (type, ref)."""
    for ats_type, pattern in ATS_PATTERNS:
        for match in pattern.finditer(html_text):
            ref = match.group(1).rstrip("/.")
            if ref.split(".")[0].lower() in _IGNORED_REFS:
                continue
            if ats_type == "teamtailor":
                ref = f"https://{ref}"
            return ats_type, ref
    if _TEAMTAILOR_HOSTED.search(html_text):
        parts = urlsplit(page_url)
        return "teamtailor", f"{parts.scheme}://{parts.netloc}"
    return None


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def careers_links(html_text: str, page_url: str) -> list[str]:
    """Links on a page that look like they lead to job listings, best first."""
    parser = _LinkParser()
    parser.feed(html_text)
    site = urlsplit(page_url).netloc.removeprefix("www.")
    scored: dict[str, int] = {}
    for href, text in parser.links:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(page_url, href).split("#")[0]
        host = urlsplit(url).netloc.removeprefix("www.")
        if not url.startswith("http"):
            continue
        score = 2 * bool(_CAREERS_WORDS.search(text)) + bool(_CAREERS_WORDS.search(url))
        # Stay on the company's own site (or its subdomains, e.g. career.example.com).
        if score and (host == site or host.endswith("." + site)):
            scored[url] = max(score, scored.get(url, 0))
    return sorted(scored, key=lambda u: -scored[u])


def _usable(found: tuple[str, str] | None, client: PoliteClient) -> bool:
    """Teamtailor refs are guessed from page origins, so confirm the feed exists."""
    if found is None:
        return False
    if found[0] != "teamtailor":
        return True
    try:
        feed = client.get_json(found[1] + "/jobs.json")
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(feed, dict) and "items" in feed


def detect(company: Company, client: PoliteClient) -> Detection:
    if company.ats is not None:
        return Detection(company.ats.type, company.ats.ref, company.careers_url)
    start = company.careers_url or company.website
    if not start:
        return Detection(None, None, None, "no website or careers_url in companies.yaml")

    try:
        resp = client.get(start)
    except httpx.HTTPError as exc:
        return Detection(None, None, None, f"{start}: {exc}")
    page_url, html_text = str(resp.url), resp.text
    found = find_ats(html_text, page_url)
    if _usable(found, client):
        return Detection(*found, careers_url=page_url)  # type: ignore[misc]

    errors: list[str] = []
    for url in careers_links(html_text, page_url)[:MAX_CAREERS_PAGES]:
        try:
            resp = client.get(url)
        except httpx.HTTPError as exc:
            errors.append(f"{url}: {exc}")
            continue
        found = find_ats(resp.text, str(resp.url))
        if _usable(found, client):
            return Detection(*found, careers_url=str(resp.url))  # type: ignore[misc]
        # One more hop: careers landing pages often link to the actual job list.
        for deeper in careers_links(resp.text, str(resp.url))[:2]:
            if deeper == url:
                continue
            try:
                inner = client.get(deeper)
            except httpx.HTTPError as exc:
                errors.append(f"{deeper}: {exc}")
                continue
            found = find_ats(inner.text, str(inner.url))
            if _usable(found, client):
                return Detection(*found, careers_url=str(inner.url))  # type: ignore[misc]
    error = "no known ATS found" + (f" ({'; '.join(errors[:2])})" if errors else "")
    return Detection(None, None, None, error)
