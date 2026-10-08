"""Find an employer's own website and the pages on it likely to name people
(docs/m5-contacts.md, 5b). Everything goes through PoliteClient: per-host pacing, and
no local or private addresses."""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from jobsearcher.companies.detect import _LinkParser
from jobsearcher.companies.http import PoliteClient
from jobsearcher.contacts.links import plain_company
from jobsearcher.models import Job

log = logging.getLogger(__name__)

MAX_PAGES = 6

# Hosts that aren't an employer's own site: job boards, ATS vendors, webmail.
NOT_COMPANY = (
    "linkedin.com", "indeed.com", "indeed.se", "arbetsformedlingen.se", "jobtechdev.se",
    "glassdoor.com", "blocket.se", "monster.se", "stepstone.se", "jobbsafari.se",
    "teamtailor.com", "varbi.com", "myworkdayjobs.com", "successfactors.com",
    "successfactors.eu", "reachmee.com", "jobylon.com", "lever.co", "greenhouse.io",
    "smartrecruiters.com", "workable.com", "recruitee.com", "hr-manager.net",
    "aplitrak.com", "contactrh.com", "talentech.com", "emply.com", "jobbnorge.no",
    "visma.com", "brightrecruiter.com", "career.se", "offentligajobb.se",
    "gmail.com", "hotmail.com", "outlook.com", "live.se", "live.com", "yahoo.com",
    "icloud.com", "me.com", "telia.com", "bredband.net", "comhem.se",
)  # fmt: skip
# Two-label public suffixes, so "acme.co.uk" stays whole (Sweden: mostly .se/.com).
_TWO_LABEL = {"co.uk", "org.uk", "com.au", "co.nz", "com.br", "co.jp"}

PAGE_WORDS = re.compile(
    r"kontakt|contact|om-?oss|about|team|ledning|management|leadership|medarbetare|"
    r"people|personal|press|media|nyheter|karri[aä]r|career|jobb|jobs|organisation|"
    r"styrelse|board|rekryter|recruit",
    re.I,
)


def registered_domain(host: str) -> str:
    """'jobs.acme.se' -> 'acme.se'."""
    labels = host.lower().strip(".").removeprefix("www.").split(".")
    keep = 3 if ".".join(labels[-2:]) in _TWO_LABEL else 2
    return ".".join(labels[-keep:])


def is_company_host(host: str) -> bool:
    domain = registered_domain(host)
    return (
        bool(domain)
        and "." in domain
        and not any(domain == bad or domain.endswith("." + bad) for bad in NOT_COMPANY)
    )


def candidate_domains(job: Job, known: dict[str, str]) -> list[tuple[str, str]]:
    """(domain, where it came from) to try for the job's employer, best first.
    `known` maps a company key to the website in a profile's companies.yaml."""
    out: list[tuple[str, str]] = []

    def add(url_or_host: str | None, source: str) -> None:
        if not url_or_host:
            return
        host = urlsplit(url_or_host if "//" in url_or_host else f"//{url_or_host}").hostname
        if host and is_company_host(host):
            domain = registered_domain(host)
            if domain not in (d for d, _ in out):
                out.append((domain, source))

    website = known.get(company_key(job.company or ""))
    add(website, "companies.yaml")
    add(job.company_url, "job source")
    if job.apply_email and "@" in job.apply_email:
        add(job.apply_email.rsplit("@", 1)[1], "application email")
    for c in job.contacts:
        if c.email and "@" in c.email:
            add(c.email.rsplit("@", 1)[1], "contact email in the ad")
    add(job.apply_url, "application link")
    add(job.url, "ad link")
    return out


def company_key(name: str) -> str:
    """A company's name, normalised to match across sources."""
    plain = unicodedata.normalize("NFKD", plain_company(name)).encode("ascii", "ignore")
    return re.sub(r"[^a-z0-9]+", " ", plain.decode().lower()).strip()


_TAGS = re.compile(r"<(script|style|noscript|svg)\b.*?</\1>|<[^>]+>", re.S | re.I)


def page_text(html_text: str) -> str:
    """The visible text of a page, whitespace collapsed."""
    return " ".join(html.unescape(_TAGS.sub(" ", html_text)).split())


def names_company(text: str, company: str) -> bool:
    """Does the page text mention the company (its main word at least)?"""
    key = company_key(company)
    words = [w for w in key.split() if len(w) >= 3] or key.split()
    haystack = company_key(text)
    return bool(words) and words[0] in haystack.split()


def verify(domain: str, company: str, client: PoliteClient) -> tuple[str, str] | None:
    """(home page URL, HTML) if `domain` answers and names the company."""
    for url in (f"https://{domain}/", f"https://www.{domain}/"):
        try:
            body = client.get_text(url)
        except httpx.HTTPError:
            continue
        if names_company(page_text(body)[:20000], company):
            return url, body
    return None


def find_site(
    job: Job,
    known: dict[str, str],
    client: PoliteClient,
    guess: Callable[[str], str | None] | None = None,
) -> tuple[str, str, str, str] | None:
    """(domain, source, home page URL, its HTML) for the job's employer, or None.
    `guess` asks a model for the domain when nothing in the job points to one."""
    company = job.company or ""
    if not company:
        return None
    candidates = candidate_domains(job, known)
    tried: set[str] = set()
    for attempt in range(2):
        if attempt == 1:  # nothing in the job pointed to the site: ask the model once
            guessed = guess(company) if guess is not None else None
            host = urlsplit(guessed if guessed and "//" in guessed else f"//{guessed}").hostname
            candidates = (
                [(registered_domain(host), "model guess")] if host and is_company_host(host) else []
            )
        for domain, source in candidates:
            if domain in tried:
                continue
            tried.add(domain)
            home = verify(domain, company, client)
            if home is not None:
                return domain, source, *home
            log.info("%s: %s (%s) doesn't look like its website", company, domain, source)
    return None


def contact_pages(home_url: str, home_html: str, client: PoliteClient) -> list[tuple[str, str]]:
    """The home page and up to MAX_PAGES - 1 linked pages on the same site that look
    like contact, about, team or press pages, as (url, html)."""
    parser = _LinkParser()
    parser.feed(home_html)
    site = registered_domain(urlsplit(home_url).hostname or "")
    scored: dict[str, int] = {}
    for href, text in parser.links:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(home_url, href).split("#")[0]
        host = urlsplit(url).hostname or ""
        if not url.startswith("http") or registered_domain(host) != site or url == home_url:
            continue
        score = 2 * bool(PAGE_WORDS.search(text)) + bool(PAGE_WORDS.search(urlsplit(url).path))
        if score:
            scored[url] = max(score, scored.get(url, 0))
    pages = [(home_url, home_html)]
    for url in sorted(scored, key=lambda u: -scored[u])[: MAX_PAGES - 1]:
        try:
            pages.append((url, client.get_text(url)))
        except httpx.HTTPError as exc:
            log.info("contact page %s failed: %s", url, exc)
    return pages
