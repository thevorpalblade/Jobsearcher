"""Recent news about a company, from Google News RSS or the GDELT DOC 2.0 API.

Google News covers Swedish media far better, but its robots.txt disallows the RSS
endpoints, so it's only used when `crawl.respect_robots` is off. GDELT is an open
index built for programmatic access (docs:
https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/), with a strict rate limit.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from jobsearcher.companies.config import Company
from jobsearcher.sources.ats.common import AtsClient

API = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 75
# GDELT rejects quoted phrases shorter than this ("The specified phrase is too short").
MIN_PHRASE = 4


def news_query(company: Company) -> str | None:
    """The company's own `news_query` as is, else its quoted name in Swedish media.
    None when the name is too short for GDELT and no `news_query` is set."""
    if company.news_query:
        return company.news_query
    if len(company.name) < MIN_PHRASE:
        return None
    return f"{company.query} sourcecountry:sweden"


class NewsQueryError(ValueError):
    pass


def fetch_news(
    client: AtsClient, company: Company, days: int, now: datetime | None = None
) -> list[dict[str, str | None]]:
    """News items (as rows for `Store.save_news_items`) from the last `days` days."""
    query = news_query(company)
    if query is None:
        raise NewsQueryError("name too short for GDELT; set news_query in companies.yaml")
    text = client.get_text(
        API,
        {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "timespan": f"{days}d",
            "maxrecords": MAX_RECORDS,
            "sort": "datedesc",
        },
    )
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        # GDELT answers errors (bad query, rate limit) with plain text.
        raise NewsQueryError(f"GDELT: {' '.join(text.split())[:150]}") from exc
    fetched = (now or datetime.now(UTC)).isoformat()
    items = []
    for article in (data or {}).get("articles") or []:
        url, title = article.get("url"), " ".join((article.get("title") or "").split())
        if not url or not title:
            continue
        items.append(
            {
                "id": hashlib.sha256(url.encode()).hexdigest()[:16],
                "company": company.slug,
                "title": title,
                "url": url,
                "domain": article.get("domain"),
                "published_at": _seendate(article.get("seendate")),
                "fetched_at": fetched,
            }
        )
    return items


def _seendate(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).isoformat()
    except ValueError:
        return None


GOOGLE_NEWS = "https://news.google.com/rss/search"


def google_query(company: Company, days: int) -> str:
    """The company's news query without GDELT-only operators, limited to `days`."""
    query = company.news_query or company.query
    query = re.sub(r"\bsourcecountry:\S+", "", query).strip()
    return f"{query} when:{days}d"


def fetch_google_news(
    client: AtsClient, company: Company, days: int, now: datetime | None = None
) -> list[dict[str, str | None]]:
    """News items from Google News RSS (Swedish edition) from the last `days` days."""
    text = client.get_text(
        GOOGLE_NEWS,
        {"q": google_query(company, days), "hl": "sv", "gl": "SE", "ceid": "SE:sv"},
    )
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise NewsQueryError(f"Google News: not RSS ({' '.join(text.split())[:100]})") from exc
    fetched = (now or datetime.now(UTC)).isoformat()
    items = []
    for item in root.iter("item"):
        url, title = item.findtext("link"), " ".join((item.findtext("title") or "").split())
        if not url or not title:
            continue
        source = item.find("source")
        publisher = (source.text or "").strip() if source is not None else ""
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3]  # Google appends " - <publisher>"
        published = None
        if item.findtext("pubDate"):
            try:
                published = parsedate_to_datetime(item.findtext("pubDate")).isoformat()
            except (TypeError, ValueError):
                published = None
        items.append(
            {
                "id": hashlib.sha256(url.encode()).hexdigest()[:16],
                "company": company.slug,
                "title": title,
                "url": url,
                "domain": publisher or None,
                "published_at": published,
                "fetched_at": fetched,
            }
        )
    return items
