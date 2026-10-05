"""ReachMee (Sweco, Bravida, Regeringskansliet, Göteborgs universitet, Malmö stad):
each customer's `main` page lists every job in a table, given the customer's `site`
and `validator` parameters. `ref` is any ReachMee URL of the customer with those
parameters (detection takes it from the careers page's links).

Columns differ per customer, so they're matched by header (Swedish or English).
The ad text is on each job's page, fetched only for wanted jobs.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from urllib.parse import parse_qs, urlencode, urlsplit

from jobsearcher.companies.config import Company
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.sources.ats.common import (
    AtsClient,
    Wanted,
    feed_source,
    html_to_text,
    swedish_city,
)
from jobsearcher.sources.base import parse_datetime

_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t([dh])\b[^>]*>(.*?)</t\1>", re.S | re.I)
_LINK = re.compile(r"""<a\b[^>]*href=['"]([^'"]+)['"][^>]*>(.*?)</a>""", re.S | re.I)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Header words -> field, in Swedish and English.
_COLUMNS = {
    "location": ("ort", "city", "location", "placering", "arbetsort", "kommun"),
    "region": ("län", "county", "region"),
    "published": ("publicerat", "published", "publiceringsdatum"),
    "deadline": ("sista ansökningsdag", "sista dag", "last application", "deadline", "apply by"),
}


def main_url(ref: str) -> tuple[str, str]:
    """The customer's job-list URL and its feed key, from any of its ReachMee URLs."""
    parts = urlsplit(ref if "://" in ref else f"https://{ref}")
    match = re.match(r"(/ext/[A-Za-z0-9]+/\d+)", parts.path)
    query = parse_qs(html.unescape(parts.query))
    if not match or "site" not in query or "validator" not in query:
        raise ValueError(f"ReachMee link without site/validator: {ref!r}; re-detect the company")
    params = {
        "site": query["site"][0],
        "lang": (query.get("lang") or ["SE"])[0],
        "validator": query["validator"][0],
    }
    base = f"https://{parts.netloc}{match.group(1)}"
    return f"{base}/main?{urlencode(params)}", f"{parts.netloc}{match.group(1)}-{params['site']}"


def _text(cell: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split())


def parse_table(page: str) -> list[dict[str, str]]:
    """Rows of the job table as {"title", "url", "location", "region", ...}."""
    columns: dict[int, str] = {}
    rows: list[dict[str, str]] = []
    for row in _ROW.findall(page):
        cells = _CELL.findall(row)
        if cells and all(kind.lower() == "h" for kind, _ in cells):
            for i, (_, cell) in enumerate(cells):
                label = _text(cell).casefold()
                for field, words in _COLUMNS.items():
                    if any(label.startswith(w) for w in words) and field not in columns.values():
                        columns[i] = field
            continue
        link = _LINK.search(cells[0][1]) if cells else None
        if not link:
            continue
        values = {"url": html.unescape(link.group(1)), "title": _text(link.group(2))}
        for i, (_, cell) in enumerate(cells):
            if i in columns:
                text = _text(cell)
                if columns[i] in ("published", "deadline"):
                    date = _DATE.search(text)
                    text = date.group(0) if date else ""
                values[columns[i]] = text
        rows.append(values)
    return rows


def fetch_jobs(client: AtsClient, ref: str, company: Company, wanted: Wanted) -> Iterator[Job]:
    url, key = main_url(ref)
    source = feed_source("reachmee", key)
    for row in parse_table(client.get_text(url)):
        job = to_job(row, company, source)
        if job is None or not wanted(job):
            continue
        page = client.get_text(job.url)  # type: ignore[arg-type]
        yield job.model_copy(update={"description": ad_text(page)})


def ad_text(page: str) -> str:
    start = page.find("jobad-body")
    if start < 0:
        return ""
    start = page.find(">", start) + 1
    return html_to_text(page[start:])


def to_job(row: dict[str, str], company: Company, source: str) -> Job | None:
    match = re.search(r"job_id=(\d+)", row.get("url", ""))
    if not match or not row.get("title"):
        return None
    source_id = match.group(1)
    location = row.get("location") or ""
    return Job(
        id=make_job_id(source, source_id),
        title=row["title"],
        company=company.name,
        company_org_nr=company.org_nr,
        location=swedish_city(location) or location or company.location,
        region=row.get("region") or None,
        url=row["url"],
        apply_url=row["url"],
        published_at=parse_datetime(row.get("published")),
        deadline=parse_datetime(row.get("deadline")),
        sources=[SourceRef(source=source, source_id=source_id, url=row["url"])],
    )
