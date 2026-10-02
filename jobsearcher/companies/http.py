"""A polite HTTP client for crawling company sites and ATS feeds: obeys robots.txt
and keeps a minimum interval between requests to the same host."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from jobsearcher.sources.base import USER_AGENT, make_client

log = logging.getLogger(__name__)

# ATS APIs are made for machine access and have no robots.txt worth reading.
_API_HOSTS = {
    "api.lever.co",
    "boards-api.greenhouse.io",
    "api.smartrecruiters.com",
    "apply.workable.com",
    "api.gdeltproject.org",
}
# Hosts that ask for a slower pace than min_interval_s.
# (GDELT asks for at most one request every 5 s, and counts strictly.)
_HOST_INTERVALS = {"api.gdeltproject.org": 6.0}


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

    def _interval(self, host: str) -> float:
        return max(self.min_interval_s, _HOST_INTERVALS.get(host, 0.0))

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            delay = self._interval(host) - (self._clock() - last)
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

    def get(self, url: str, params: dict | None = None, retries: int = 3) -> httpx.Response:
        """GET politely, retrying transient failures (network errors, 429, 5xx) with
        backoff that starts at the host's own pace."""
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        delay = max(2.0, self._interval(host))
        for attempt in range(retries + 1):
            self._wait(host)
            try:
                resp = self.client.get(url, params=params)
            except httpx.TransportError:
                if attempt == retries:
                    raise
            else:
                if resp.status_code != 429 and resp.status_code < 500 or attempt == retries:
                    resp.raise_for_status()
                    return resp
            log.info("GET %s failed; retrying in %.0fs", url, delay)
            self._sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self.get(url, params).text

    def get_json(self, url: str, params: dict | None = None) -> Any:
        return self.get(url, params).json()
