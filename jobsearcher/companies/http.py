"""A polite HTTP client for crawling company sites and ATS feeds: obeys robots.txt
and keeps a minimum interval between requests to the same host."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from jobsearcher.sources.base import USER_AGENT, get_json, make_client

log = logging.getLogger(__name__)

# ATS APIs are made for machine access and have no robots.txt worth reading.
_API_HOSTS = {
    "api.lever.co",
    "boards-api.greenhouse.io",
    "api.smartrecruiters.com",
    "apply.workable.com",
}


class RobotsDisallowed(httpx.HTTPError):
    pass


class PoliteClient:
    def __init__(
        self,
        client: httpx.Client | None = None,
        min_interval_s: float = 1.0,
        sleep: Any = time.sleep,
        clock: Any = time.monotonic,
    ):
        self.client = client or make_client(
            {"Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8"}
        )
        self.min_interval_s = min_interval_s
        self._sleep, self._clock = sleep, clock
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            delay = self.min_interval_s - (self._clock() - last)
            if delay > 0:
                self._sleep(delay)
        self._last[host] = self._clock()

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.hostname in _API_HOSTS:
            return True
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                self._wait(parts.netloc)
                resp = self.client.get(f"{origin}/robots.txt")
                if resp.status_code >= 400:
                    parser = None  # no robots.txt: everything allowed
                else:
                    parser.parse(resp.text.splitlines())
            except httpx.HTTPError:
                parser = None
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(USER_AGENT, url)

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        self._wait(urlsplit(url).netloc)
        resp = self.client.get(url, params=params)
        resp.raise_for_status()
        return resp

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self.get(url, params).text

    def get_json(self, url: str, params: dict | None = None) -> Any:
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        self._wait(urlsplit(url).netloc)
        # get_json retries transient failures (429, 5xx, network errors).
        return get_json(self.client, url, params or {})
