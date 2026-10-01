"""Recent news about a company from the GDELT DOC 2.0 API: an open news index built for
programmatic access (Google News' robots.txt disallows its RSS endpoints).
Docs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
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
