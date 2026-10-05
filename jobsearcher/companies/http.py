"""The HTTP client for crawling company sites, ATS feeds and news: keeps a minimum
interval between requests to the same host, and obeys robots.txt unless the
`crawl.respect_robots` setting turns that off."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from jobsearcher.config import Config
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


_BROWSER_SET = {
    "accept", "accept-encoding", "accept-language", "connection", "host", "user-agent",
}  # fmt: skip


class BrowserTransport(httpx.BaseTransport):
    """Sends httpx requests through curl_cffi impersonating Chrome. Bot protection
    (Akamai, Cloudflare) fingerprints the TLS handshake, so a browser User-Agent
    header alone still gets 403 from sites like Volvo Cars, Ericsson and PostNord.
    Redirects are left to httpx, so callers still see the final URL."""

    def __init__(self, impersonate: str = "chrome"):
        from curl_cffi import requests as curl_requests

        self.session = curl_requests.Session(impersonate=impersonate)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        from curl_cffi.requests.exceptions import RequestException

        try:
            resp = self.session.request(
                request.method,
                str(request.url),
                # Chrome's own headers come from the impersonation; replacing them with
                # httpx's defaults (Accept, User-Agent, ...) gives the imitation away.
                headers={k: v for k, v in request.headers.items() if k.lower() not in _BROWSER_SET},
                data=request.read() or None,
                allow_redirects=False,
                timeout=30,
            )
        except RequestException as exc:
            raise httpx.ConnectError(str(exc), request=request) from exc
        # curl_cffi already decompressed the body; don't let httpx decode it again.
        headers = [
            (k, v)
            for k, v in resp.headers.items()
            if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        ]
        return httpx.Response(
            resp.status_code, headers=headers, content=resp.content, request=request
        )

    def close(self) -> None:
        self.session.close()


def browser_transport() -> BrowserTransport | None:
    """A Chrome-impersonating transport, if curl_cffi is installed (the `jobspy` extra)."""
    try:
        return BrowserTransport()
    except ImportError:
        return None


class PoliteClient:
    def __init__(
        self,
        client: httpx.Client | None = None,
        min_interval_s: float = 1.0,
        user_agent: str = USER_AGENT,
        respect_robots: bool = True,
        sleep: Any = time.sleep,
        clock: Any = time.monotonic,
    ):
        self.client = client or make_client(
            {
                "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
                "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
            },
            user_agent=user_agent,
        )
        self.user_agent = user_agent
        self.respect_robots = respect_robots
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

    @classmethod
    def from_config(cls, config: Config) -> PoliteClient:
        polite = cls(
            min_interval_s=config.companies.min_request_interval_s,
            user_agent=config.crawl.user_agent_string,
            respect_robots=config.crawl.respect_robots,
        )
        if config.crawl.user_agent == "chrome":
            transport = browser_transport()
            if transport is None:
                log.info("curl_cffi not installed: sending a Chrome User-Agent header only")
            else:
                headers = polite.client.headers
                polite.client = httpx.Client(
                    transport=transport, headers=headers, timeout=30.0, follow_redirects=True
                )
        return polite

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if not self.respect_robots or parts.hostname in _API_HOSTS:
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
        return parser is None or parser.can_fetch(self.user_agent, url)

    def request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        retries: int = 3,
    ) -> httpx.Response:
        """Send politely, retrying transient failures (network errors, 429, 5xx) with
        backoff that starts at the host's own pace."""
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        delay = max(2.0, self._interval(host))
        for attempt in range(retries + 1):
            self._wait(host)
            try:
                resp = self.client.request(method, url, params=params, json=json, headers=headers)
            except httpx.TransportError:
                if attempt == retries:
                    raise
            else:
                if resp.status_code != 429 and resp.status_code < 500 or attempt == retries:
                    resp.raise_for_status()
                    return resp
            log.info("%s %s failed; retrying in %.0fs", method, url, delay)
            self._sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")

    def get(
        self, url: str, params: dict | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        return self.request("GET", url, params=params, headers=headers)

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self.get(url, params).text

    def get_json(
        self, url: str, params: dict | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        return self.get(url, params, headers).json()

    def post_json(self, url: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        return self.request("POST", url, json=body, headers=headers).json()
