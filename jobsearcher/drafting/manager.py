"""Runs drafts in the background for the web UI, one at a time.

A draft takes a minute or two (an Opus call, a check, maybe a repair round), so the web
request only queues it; the page polls for the result. One worker thread keeps the
subscription's usage limits and the machine from being hit by several at once.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jobsearcher.config import Config
from jobsearcher.drafting.core import Draft
from jobsearcher.store import Store

log = logging.getLogger(__name__)

Task = Callable[[Config, Store], Draft]


@dataclass
class Status:
    state: str  # queued | running | done | failed
    error: str | None = None
    queued_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")


class DraftManager:
    def __init__(self, get_config: Callable[[], Config], db_path: Path):
        self._get_config = get_config
        self._db_path = db_path
        self._lock = threading.Lock()
        self._status: dict[str, Status] = {}
        self._queue: queue.Queue[tuple[str, Task]] = queue.Queue()
        self._worker: threading.Thread | None = None

    def status(self, key: str) -> Status | None:
        return self._status.get(key)

    def pending(self) -> int:
        return sum(s.active for s in self._status.values())

    def submit(self, key: str, task: Task) -> Status:
        """Queue a draft for `key`; if one is already queued or running, return it."""
        with self._lock:
            current = self._status.get(key)
            if current is not None and current.active:
                return current
            status = self._status[key] = Status("queued", queued_at=datetime.now(UTC))
            self._queue.put((key, task))
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name="drafts", daemon=True)
                self._worker.start()
            return status

    def _work(self) -> None:
        while True:
            try:
                key, task = self._queue.get(timeout=30)
            except queue.Empty:
                with self._lock:  # a submit can't slip in between this check and exiting
                    if self._queue.empty():
                        self._worker = None
                        return  # idle: the next submit starts a new worker
                continue
            status = self._status[key]
            status.state = "running"
            store = Store(self._db_path)
            try:
                task(self._get_config(), store)
                status.state = "done"
            except Exception as exc:  # shown to the user; the worker must carry on
                log.warning("Draft for %s failed: %s", key, exc)
                status.state, status.error = "failed", str(exc) or type(exc).__name__
            finally:
                store.close()
