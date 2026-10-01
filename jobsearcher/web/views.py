"""Data shaping for the web UI: loading, scoring, filtering and sorting job rows.

No FastAPI here, so it can be tested and reused on its own. Scores and the prefilter
are computed in Python on every request (`final_score` reads the current
ranking.yaml); that's fast enough for a few thousand jobs, especially with the
prefilter memoised per job.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Generic, Literal, TypeVar

from jobsearcher.models import Job, JobStatus
from jobsearcher.ranking.config import RankingConfig, load_ranking_config
from jobsearcher.ranking.prefilter import PrefilterResult, prefilter_status
from jobsearcher.ranking.ranker import Ranking, ScoreBreakdown, input_hash, score_breakdown
from jobsearcher.store import JobRecord, Store

T = TypeVar("T")

Stage = Literal["ranked", "pending", "excluded"]
# How many expired jobs view=expired shows (they accumulate forever).
EXPIRED_LIMIT = 200


class MtimeCache(Generic[T]):
    """A file's parsed contents, reloaded when its modification time changes."""

    def __init__(self, path: Path, load: Callable[[Path], T]):
        self.path = path
        self._load = load
        self._lock = threading.Lock()
        self._mtime: int | None = None
        self._value: T | None = None
        self._loaded = False

    def get(self) -> T:
        try:
            mtime: int | None = self.path.stat().st_mtime_ns
        except OSError:
            mtime = None
        with self._lock:
            if not self._loaded or mtime != self._mtime:
                self._value = self._load(self.path)
                self._mtime, self._loaded = mtime, True
            return self._value  # type: ignore[return-value]


def ranking_config_cache(path: Path) -> MtimeCache[RankingConfig]:
    return MtimeCache(path, load_ranking_config)


def cv_cache(path: Path) -> MtimeCache[str | None]:
    # Only used to flag stale rankings; without a CV nothing is flagged.
    return MtimeCache(path, lambda p: p.read_text() if p.is_file() else None)


class PrefilterMemo:
    """prefilter_status() per job, kept across requests. The key covers everything
    the result depends on; entries for an old ranking.yaml are dropped when the
    filter fingerprint changes, so the memo never outgrows the job table."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fingerprint: str | None = None
        self._results: dict[tuple[str, str, str | None, str | None], PrefilterResult] = {}

    def get(self, record: JobRecord, config: RankingConfig, fingerprint: str) -> PrefilterResult:
        job = record.job
        key = (job.id, job.content_hash, job.occupation_field, job.occupation_group)
        with self._lock:
            if fingerprint != self._fingerprint:
                self._results.clear()
                self._fingerprint = fingerprint
            result = self._results.get(key)
        if result is None:
            result = prefilter_status(job, config)
            with self._lock:
                if fingerprint == self._fingerprint:
                    self._results[key] = result
        return result


@dataclass
class JobRow:
    record: JobRecord
    ranking: Ranking | None
    score: int | None
    breakdown: ScoreBreakdown | None
    prefilter: PrefilterResult
    stage: Stage
    stale: bool = False
    # A stored ranking that no longer parses (older prompt version); it will be re-ranked.
    unparseable: bool = False
    days_left: int | None = None  # until the deadline; negative once past

    @property
    def job(self) -> Job:
        return self.record.job


@dataclass
class RowContext:
    """Everything load_rows() needs besides the store, gathered once per request."""

    config: RankingConfig
    memo: PrefilterMemo
    cv: str | None
    model: str  # the configured ranking model, for the stale check
    now: datetime
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        self.fingerprint = self.config.filter_fingerprint()


def make_row(record: JobRecord, ranking_json: str | None, ctx: RowContext) -> JobRow:
    job = record.job
    prefilter = ctx.memo.get(record, ctx.config, ctx.fingerprint)
    ranking: Ranking | None = None
    unparseable = False
    if ranking_json is not None:
        try:
            ranking = Ranking.model_validate_json(ranking_json)
        except ValueError:
            unparseable = True
    breakdown = score_breakdown(ranking.assessment, ctx.config) if ranking else None
    stage: Stage = "ranked" if ranking else ("pending" if prefilter.passed else "excluded")
    stale = bool(
        ranking
        and ctx.cv is not None
        and ranking.input_hash != input_hash(job, ctx.cv, ctx.config, ctx.model)
    )
    days_left = None
    if job.deadline is not None:
        deadline = job.deadline if job.deadline.tzinfo else job.deadline.replace(tzinfo=UTC)
        days_left = (deadline.date() - ctx.now.date()).days
    return JobRow(
        record=record,
        ranking=ranking,
        score=breakdown.total if breakdown else None,
        breakdown=breakdown,
        prefilter=prefilter,
        stage=stage,
        stale=stale,
        unparseable=unparseable,
        days_left=days_left,
    )


def load_rows(store: Store, ctx: RowContext, view: str = "ranked") -> list[JobRow]:
    """Rows for a list view. Open jobs normally; expired ones only when asked for."""
    if view == "expired":
        records = store.job_records(JobStatus.EXPIRED, limit=EXPIRED_LIMIT)
        rankings = store.latest_rankings(status=JobStatus.EXPIRED)
    else:
        records = store.job_records(JobStatus.OPEN)
        rankings = store.latest_rankings(status=JobStatus.OPEN)
    return [make_row(r, rankings.get(r.job.id), ctx) for r in records]


def _published(row: JobRow) -> float:
    p = row.job.published_at or row.record.first_seen
    return (p if p.tzinfo else p.replace(tzinfo=UTC)).timestamp()


_SORT_VALUES: dict[str, Callable[[JobRow], int | None]] = {
    "score": lambda r: r.score,
    "fit": lambda r: r.ranking.assessment.fit_score if r.ranking else None,
    "success": lambda r: r.ranking.assessment.success_score if r.ranking else None,
}


def sort_rows(rows: list[JobRow], sort: str = "score") -> list[JobRow]:
    """Best (or soonest deadline) first; rows without the value go last, and ties
    keep newest ads first."""
    rows = sorted(rows, key=_published, reverse=True)
    if sort == "published":
        return rows
    if sort == "deadline":
        return sorted(rows, key=lambda r: (r.days_left is None, r.days_left or 0))
    value = _SORT_VALUES.get(sort, _SORT_VALUES["score"])
    return sorted(rows, key=lambda r: (value(r) is None, -(value(r) or 0)))
