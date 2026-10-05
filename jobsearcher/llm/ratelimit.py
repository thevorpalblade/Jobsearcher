"""Client-side rate limiting for APIs with a hard requests-per-minute cap.

NVIDIA's free hosted API allows about 40 requests per minute per API key, shared by every
model on the key, and answers 429 beyond that; its limits aren't otherwise published and
a 429 carries no Retry-After. Retries make this worse: the OpenAI client library retries
a failed request on its own, so a burst of fast failures multiplies into far more
requests (we once sent 140 a minute while being refused). So every request, retries
included, goes through one limiter per process, which spaces requests out and, after a
429, pauses everyone for a growing cooldown instead of hammering on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

# First cooldown after a 429; each further 429 in a row doubles it, up to the maximum.
FIRST_COOLDOWN_S = 30.0
MAX_COOLDOWN_S = 600.0


class RateLimiter:
    def __init__(
        self,
        requests_per_minute: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.interval = 60.0 / requests_per_minute
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._next_slot = 0.0  # earliest time the next request may start
        self._cooldown_until = 0.0
        self._streak = 0  # 429s in a row

    def acquire(self) -> float:
        """Block until a request may be sent (evenly spaced, and not during a cooldown).
        Returns how long it waited."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next_slot, self._cooldown_until)
            self._next_slot = start + self.interval
        wait = start - now
        if wait > 0:
            self._sleep(wait)
        return max(wait, 0.0)

    def too_many_requests(self, retry_after: float | None = None) -> float:
        """Record a 429: everyone waits out a cooldown (the server's Retry-After when it
        gave one). Returns the cooldown in seconds."""
        with self._lock:
            self._streak += 1
            seconds = retry_after or min(FIRST_COOLDOWN_S * 2 ** (self._streak - 1), MAX_COOLDOWN_S)
            until = self._clock() + seconds
            self._cooldown_until = max(self._cooldown_until, until)
            self._next_slot = max(self._next_slot, self._cooldown_until)
            return seconds

    def succeeded(self) -> None:
        with self._lock:
            self._streak = 0

    @property
    def cooling_down(self) -> bool:
        return self._clock() < self._cooldown_until


_registry: dict[str, RateLimiter] = {}
_registry_lock = threading.Lock()


def limiter_for(name: str, requests_per_minute: float) -> RateLimiter | None:
    """The process-wide limiter for an API (ranking, news classification and drafts'
    grounding checks all share one), or None when no limit is configured."""
    if not requests_per_minute or requests_per_minute <= 0:
        return None
    with _registry_lock:
        limiter = _registry.get(name)
        if limiter is None or abs(limiter.interval - 60.0 / requests_per_minute) > 1e-9:
            limiter = _registry[name] = RateLimiter(requests_per_minute)
        return limiter
