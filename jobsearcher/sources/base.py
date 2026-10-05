"""Source adapter interface and shared HTTP helpers."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Protocol

import httpx

from jobsearcher.config import HONEST_USER_AGENT
from jobsearcher.models import Job

log = logging.getLogger(__name__)

USER_AGENT = HONEST_USER_AGENT


class SourceAdapter(Protocol):
    name: str

    def search(self, keyword: str, published_after: datetime | None) -> Iterator[Job]:
        """Yield all jobs matching `keyword`, optionally only those published after a time."""
        ...


def get_json(
    client: httpx.Client, url: str, params: dict[str, Any], retries: int = 4
) -> dict[str, Any]:
    """GET with exponential backoff on transient failures (network errors, 429, 5xx)."""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            resp = client.get(url, params=params)
            if resp.status_code != 429 and resp.status_code < 500:
                resp.raise_for_status()
                return resp.json()
            error: Exception = httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )
        except httpx.TransportError as exc:
            error = exc
        if attempt == retries:
            raise error
        log.warning("GET %s failed (%s); retrying in %.0fs", url, error, delay)
        time.sleep(delay)
        delay *= 2
    raise AssertionError("unreachable")


def make_client(
    headers: dict[str, str] | None = None, user_agent: str = USER_AGENT
) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent, "Accept": "application/json", **(headers or {})},
        timeout=30.0,
        follow_redirects=True,
    )


def parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def dig(data: Any, *path: str) -> Any:
    """Safe nested lookup: dig(hit, "employer", "name")."""
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data
