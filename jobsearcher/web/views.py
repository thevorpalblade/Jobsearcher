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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jobsearcher.config import LLMConfig
from jobsearcher.llm.budget import BudgetTracker
from jobsearcher.models import Application, ApplicationState, Job, JobStatus
from jobsearcher.ranking.config import RankingConfig, load_ranking_config
from jobsearcher.ranking.prefilter import PrefilterResult, prefilter_status
from jobsearcher.ranking.ranker import Ranking, ScoreBreakdown, input_hash, score_breakdown
from jobsearcher.store import JobRecord, Store

T = TypeVar("T")

Stage = Literal["ranked", "pending", "excluded"]
STAGES: tuple[Stage, ...] = ("ranked", "pending", "excluded")
View = Literal["ranked", "pending", "excluded", "all", "expired", "tracked"]
# How many expired jobs view=expired shows (they accumulate forever).
EXPIRED_LIMIT = 200
# Jobs first seen this recently get a "new" badge (unless the `new` filter says otherwise).
NEW_DAYS = 2


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
    age_days: float = 0.0  # since first seen
    application: Application | None = None  # tracking state; None = new

    @property
    def job(self) -> Job:
        return self.record.job

    @property
    def state(self) -> ApplicationState:
        return self.application.state if self.application else ApplicationState.NEW

    @property
    def match_place(self) -> str:
        """Where the job mentions a target role: "title", "text only" or ""."""
        p = self.prefilter
        if not p.roles_unfiltered:
            return ""
        return "text only" if set(p.roles_unfiltered) <= set(p.body_only) else "title"

    @property
    def exclusion_reason(self) -> str:
        """Why the prefilter keeps this job from ranking ("" if it passes)."""
        p = self.prefilter
        if p.passed:
            return ""
        if not p.excluded:
            return "mentions no target role"
        return "; ".join(f"{role}: {reason}" for role, reason in p.excluded.items())


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


def make_row(
    record: JobRecord,
    ranking_json: str | None,
    ctx: RowContext,
    application: Application | None = None,
) -> JobRow:
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
    first_seen = record.first_seen
    if first_seen.tzinfo is None:
        first_seen = first_seen.replace(tzinfo=UTC)
    return JobRow(
        age_days=(ctx.now - first_seen) / timedelta(days=1),
        record=record,
        ranking=ranking,
        score=breakdown.total if breakdown else None,
        breakdown=breakdown,
        prefilter=prefilter,
        stage=stage,
        stale=stale,
        unparseable=unparseable,
        application=application,
        days_left=days_left,
    )


def load_rows(store: Store, ctx: RowContext, view: str = "ranked") -> list[JobRow]:
    """Rows for a list view. Open jobs normally; expired ones only when asked for, and
    tracked jobs whether open or expired."""
    if view == "expired":
        records = store.job_records(JobStatus.EXPIRED, limit=EXPIRED_LIMIT)
        rankings = store.latest_rankings(status=JobStatus.EXPIRED)
    elif view == "tracked":
        records = store.tracked_job_records()
        rankings = store.latest_rankings(status=None)
    else:
        records = store.job_records(JobStatus.OPEN)
        rankings = store.latest_rankings(status=JobStatus.OPEN)
    apps = store.applications()
    return [make_row(r, rankings.get(r.job.id), ctx, apps.get(r.job.id)) for r in records]


SortKey = Literal[
    "score", "fit", "success", "title", "company", "location", "remote", "deadline",
    "language", "contact", "source", "tracking", "published",
]  # fmt: skip


class ListFilters(BaseModel):
    """Query parameters of the job list. Every filter is optional."""

    model_config = ConfigDict(extra="ignore")

    view: View = "ranked"
    min_score: int | None = Field(None, ge=0, le=100)
    source: str | None = None
    location: str | None = None  # substring of location or region
    remote: bool | None = None
    deadline_within: int | None = Field(None, ge=0)  # days; also hides past deadlines
    role: str | None = None  # ranked: the model's matched_role; else prefilter roles
    occupation: str | None = None  # exact occupation field or group (from /prefilter)
    language: Literal["sv", "en", "other"] | None = None
    swedish: Literal["any", "exclude_required", "not_mentioned"] = "any"
    has_contact: bool | None = None
    new: int | None = Field(None, ge=0)  # first seen within N days
    q: str | None = None  # title or company
    sort: SortKey = "score"
    dir: Literal["asc", "desc"] | None = None  # None: the column's natural direction

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, data: Any) -> Any:
        # An HTML form sends every field, empty ones as "", which would fail int/bool/
        # Literal validation; treat them as not given.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v != ""}
        return data


def _matches(row: JobRow, f: ListFilters) -> bool:
    job = row.job
    a = row.ranking.assessment if row.ranking else None
    if f.view in ("ranked", "pending", "excluded") and row.stage != f.view:
        return False
    if f.view != "tracked" and row.state == ApplicationState.IGNORED:
        return False
    if f.min_score is not None and (row.score is None or row.score < f.min_score):
        return False
    if f.source and f.source not in {s.source for s in job.sources}:
        return False
    if f.location:
        place = f"{job.location or ''} {job.region or ''}".casefold()
        if f.location.casefold() not in place:
            return False
    if f.remote is not None and bool(job.remote) != f.remote:
        return False
    if f.deadline_within is not None and (
        row.days_left is None or not 0 <= row.days_left <= f.deadline_within
    ):
        return False
    if f.role:
        roles = [a.matched_role] if a else row.prefilter.roles_unfiltered
        if f.role.casefold() not in {(r or "").casefold() for r in roles}:
            return False
    if f.occupation and f.occupation not in (job.occupation_field, job.occupation_group):
        return False
    if f.language and (a is None or a.language != f.language):
        return False
    if f.swedish == "exclude_required" and a is not None and a.swedish == "required":
        return False
    if f.swedish == "not_mentioned" and (a is None or a.swedish != "not_mentioned"):
        return False
    if f.has_contact is not None and bool(job.contacts) != f.has_contact:
        return False
    if f.new is not None and row.age_days > f.new:
        return False
    if f.q:
        text = f"{job.title} {job.company or ''}".casefold()
        if f.q.casefold() not in text:
            return False
    return True


def apply_filters(rows: list[JobRow], filters: ListFilters) -> list[JobRow]:
    return [row for row in rows if _matches(row, filters)]


@dataclass
class FilterOptions:
    """Choices for the filter dropdowns, taken from the unfiltered rows."""

    sources: list[str]
    locations: list[str]
    roles: list[str]


def filter_options(rows: list[JobRow], config: RankingConfig) -> FilterOptions:
    return FilterOptions(
        sources=sorted({s.source for row in rows for s in row.job.sources}),
        locations=sorted({row.job.location for row in rows if row.job.location}, key=str.casefold),
        roles=[role.name for role in config.target_roles],
    )


def _published(row: JobRow) -> float:
    p = row.job.published_at or row.record.first_seen
    return (p if p.tzinfo else p.replace(tzinfo=UTC)).timestamp()


def _text(value: str | None) -> str | None:
    return value.casefold() if value else None


_LANGUAGE_ORDER = {"en": 0, "other": 1, "sv": 2}
_SWEDISH_ORDER = {"not_mentioned": 0, "merit": 1, "required": 2}

# Sort key per table column; None means "no value", which always sorts last.
_SORT_VALUES: dict[str, Callable[[JobRow], Any]] = {
    "score": lambda r: r.score,
    "fit": lambda r: r.ranking.assessment.fit_score if r.ranking else None,
    "success": lambda r: r.ranking.assessment.success_score if r.ranking else None,
    "title": lambda r: _text(r.job.title),
    "company": lambda r: _text(r.job.company),
    "location": lambda r: _text(r.job.location or r.job.region),
    "remote": lambda r: 1 if r.job.remote else 0,
    "deadline": lambda r: r.days_left,
    # Easiest first: English ads, then Swedish not mentioned, a merit, required.
    "language": lambda r: (
        (
            _LANGUAGE_ORDER.get(r.ranking.assessment.language, 1),
            _SWEDISH_ORDER.get(r.ranking.assessment.swedish, 0),
        )
        if r.ranking
        else None
    ),
    "contact": lambda r: 1 if r.job.contacts else 0,
    "source": lambda r: ", ".join(sorted({s.source for s in r.job.sources})) or None,
    "tracking": lambda r: r.state.value if r.application else None,
    "published": _published,
}
# Clicking a column sorts it this way first: best, most and newest first; soonest
# deadline first; text A to Z.
_DESCENDING = {"score", "fit", "success", "remote", "contact", "published"}
SORT_LABELS = {
    "score": "Score",
    "fit": "Fit",
    "success": "Success",
    "deadline": "Deadline",
    "published": "Published",
    "title": "Title",
    "company": "Company",
    "location": "Location",
    "remote": "Remote",
    "language": "Language",
    "contact": "Contact",
    "source": "Source",
    "tracking": "Tracking",
}


def sort_direction(sort: str, direction: str | None) -> str:
    return direction or ("desc" if sort in _DESCENDING else "asc")


def sort_rows(
    rows: list[JobRow], sort: str = "score", direction: str | None = None
) -> list[JobRow]:
    """Rows by one column. Rows without a value go last either way, and ties keep
    the newest ads first."""
    value = _SORT_VALUES.get(sort, _SORT_VALUES["score"])
    rows = sorted(rows, key=_published, reverse=True)
    present = [r for r in rows if value(r) is not None]
    missing = [r for r in rows if value(r) is None]
    present.sort(key=value, reverse=sort_direction(sort, direction) == "desc")
    return present + missing


def sort_links(filters: ListFilters) -> dict[str, dict[str, str]]:
    """For each sortable column: the URL that sorts by it (keeping the other filters)
    and the arrow to show on the active one. Clicking the active column flips it."""
    params = {
        k: str(v).lower() if isinstance(v, bool) else str(v)
        for k, v in filters.model_dump(exclude_none=True).items()
        if k not in ("sort", "dir") and v != ""
    }
    current = sort_direction(filters.sort, filters.dir)
    links = {}
    for key in _SORT_VALUES:
        natural = sort_direction(key, None)
        active = key == filters.sort
        direction = ("asc" if current == "desc" else "desc") if active else natural
        query = {**params, "sort": key}
        if direction != natural:
            query["dir"] = direction
        links[key] = {
            "href": "/?" + urlencode(query),
            "arrow": ("▼" if current == "desc" else "▲") if active else "",
        }
    return links


@dataclass
class OccupationCount:
    label: str | None  # occupation field or group; None when the ad has none
    count: int
    excluded: bool  # by this role's occupation filters
    groups: list[OccupationCount] = field(default_factory=list)


@dataclass
class RoleSummary:
    name: str
    mentioned: int  # open jobs mentioning the role, before occupation filters
    kept: int
    excluded: int
    fields: list[OccupationCount]


def prefilter_summary(rows: list[JobRow], config: RankingConfig) -> list[RoleSummary]:
    """Per target role, which occupations its mentions fall in and which the role's
    filters exclude: the web version of `jobsearcher occupations --groups`."""
    out: list[RoleSummary] = []
    for role in config.target_roles:
        counts: dict[str | None, dict[str | None, int]] = {}
        for row in rows:
            if role.name in row.prefilter.roles_unfiltered:
                job = row.job
                groups = counts.setdefault(job.occupation_field, {})
                groups[job.occupation_group] = groups.get(job.occupation_group, 0) + 1
        fields: list[OccupationCount] = []
        kept = excluded = 0
        for field_label, groups in counts.items():
            group_counts = []
            for group_label, n in groups.items():
                allowed = role.allows_occupation(field_label, group_label)
                kept += n if allowed else 0
                excluded += 0 if allowed else n
                group_counts.append(OccupationCount(group_label, n, not allowed))
            group_counts.sort(key=lambda g: -g.count)
            fields.append(
                OccupationCount(
                    field_label,
                    sum(g.count for g in group_counts),
                    all(g.excluded for g in group_counts),
                    group_counts,
                )
            )
        fields.sort(key=lambda f: -f.count)
        out.append(RoleSummary(role.name, kept + excluded, kept, excluded, fields))
    return out


@dataclass
class BudgetInfo:
    spent: float  # USD this month, pay-per-token providers only
    budget: float
    drafting_limit: float  # drafting pauses here
    subscription_calls: int  # Claude Code on the subscription: not in `spent`
    subscription_tokens: int

    @property
    def percent(self) -> float:
        return min(100.0, 100 * self.spent / self.budget) if self.budget > 0 else 100.0

    @property
    def drafting_percent(self) -> float:
        return min(100.0, 100 * self.drafting_limit / self.budget) if self.budget > 0 else 0.0

    @property
    def level(self) -> str:
        """CSS class for the bar: "", "warn" (drafting paused) or "over"."""
        if self.spent >= self.budget:
            return "over"
        return "warn" if self.spent >= self.drafting_limit else ""


def budget_info(store: Store, config: LLMConfig) -> BudgetInfo:
    tracker = BudgetTracker(store, config)
    calls, tokens = tracker.subscription_usage()
    return BudgetInfo(
        spent=tracker.month_to_date(),
        budget=config.monthly_budget_usd,
        drafting_limit=tracker.limit_for("drafting"),
        subscription_calls=calls,
        subscription_tokens=tokens,
    )


def safe_url(url: str | None) -> str | None:
    """The URL if it may become a link. Ad data is untrusted, so anything but http(s)
    (javascript:, data:, ...) is shown as text instead."""
    if not url:
        return None
    url = url.strip()
    return url if urlsplit(url).scheme.lower() in ("http", "https") else None


_PROVENANCE_LABELS = {
    "platsbanken:application_contacts": "Platsbanken (structured)",
    "platsbanken:ad_text": "Found in the ad text",
    "ad_text": "Found in the ad text",
    "llm:ad_text": "Named in the ad (extracted by the LLM, check before use)",
}


def provenance_label(provenance: str) -> str:
    """A readable label for a contact's provenance (the raw value is shown too)."""
    return _PROVENANCE_LABELS.get(provenance, provenance)
