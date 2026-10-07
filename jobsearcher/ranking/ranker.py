"""LLM scoring of jobs against the candidate's CVs."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from jobsearcher.llm import BudgetedLLM, BudgetExceeded, LLMError, LLMResult
from jobsearcher.models import Contact, Job
from jobsearcher.ranking.config import RankingConfig
from jobsearcher.ranking.prefilter import select_for_ranking
from jobsearcher.store import Store, merge_contacts

log = logging.getLogger(__name__)

# Bump when the prompt or schema changes in a way that should invalidate old rankings.
PROMPT_VERSION = "3"


class ContactPerson(BaseModel):
    # No defaults: every field is required (nullable), as strict structured output expects.
    name: str
    role: str | None
    email: str | None
    phone: str | None


class JobAssessment(BaseModel):
    """What the ranking model returns for one job."""

    fit_score: int = Field(ge=0, le=100, description="Experience/skills match, 0-100")
    success_score: int = Field(
        ge=0, le=100, description="Estimated chance of being invited to interview, 0-100"
    )
    matched_role: str | None = Field(description="Which target role this job is, if any")
    matched_requirements: list[str]
    missing_requirements: list[str]
    red_flags: list[str]
    rationale: str = Field(description="2-4 sentences explaining the scores")
    language: Literal["sv", "en", "other"] = Field(description="Language the ad is written in")
    swedish: Literal["required", "merit", "not_mentioned"] = Field(
        description="Whether the ad asks for Swedish language skills: 'required' if it is a "
        "stated requirement, 'merit' if only a plus, else 'not_mentioned'"
    )
    contact_persons: list[ContactPerson] = Field(
        description="Named people in the ad to contact about the role (hiring manager, "
        "recruiter). Empty if none are named."
    )


class Ranking(BaseModel):
    job_id: str
    input_hash: str
    model: str
    assessment: JobAssessment


class ScoreBreakdown(BaseModel):
    """How a final score is put together, for display."""

    fit: int
    success: int
    fit_weight: float
    success_weight: float
    combined: int  # weighted fit/success, before adjustments
    adjustments: list[tuple[str, int]]  # (label, points), only the ones that apply
    total: int  # clamped to 0-100


def score_breakdown(assessment: JobAssessment, config: RankingConfig) -> ScoreBreakdown:
    """Combined score from the current weights and adjustments, step by step."""
    combined = config.weights.combine(assessment.fit_score, assessment.success_score)
    adj = config.adjustments
    lines: list[tuple[str, int]] = []
    if assessment.language == "en":
        lines.append(("Ad written in English", adj.english_ad))
    if assessment.swedish == "required":
        lines.append(("Swedish required", adj.swedish_required))
    elif assessment.swedish == "merit":
        lines.append(("Swedish a merit", adj.swedish_merit))
    total = combined + sum(points for _, points in lines)
    return ScoreBreakdown(
        fit=assessment.fit_score,
        success=assessment.success_score,
        fit_weight=config.weights.fit,
        success_weight=config.weights.success,
        combined=combined,
        adjustments=lines,
        total=max(0, min(100, total)),
    )


def final_score(assessment: JobAssessment, config: RankingConfig) -> int:
    """Combined score from the current weights and adjustments (computed on read, so
    editing them in ranking.yaml takes effect without re-ranking)."""
    return score_breakdown(assessment, config).total


SYSTEM_PROMPT = """\
You assess job ads for one candidate. Their CVs (the master CV, then any other CVs of \
the same person) and preferences follow.
Score each ad honestly: a high score for a poor match wastes the candidate's time.

Scoring rubric:
- fit_score: how well the candidate's documented experience matches the ad's stated \
requirements. 90+ = meets essentially all requirements; 70 = meets the core requirements \
with gaps in nice-to-haves; 50 = partial match; below 30 = different profession.
- success_score: realistic chance of an interview invitation, considering seniority gap, \
missing must-haves, language requirements, and location. Be conservative.
- Any dealbreaker from the preferences caps both scores at 10.
- Only use facts from the CVs. Do not assume experience that isn't written there.
- contact_persons: only people explicitly named in the ad text. Never invent contacts.
- language and swedish are facts about the ad; report them accurately. Language \
preferences are applied separately, so don't also fold them into fit_score.
"""


def build_context(cv: str, config: RankingConfig) -> str:
    """Stable part of every ranking prompt (cached by the providers)."""
    p = config.preferences
    lines = ["# Target roles"]
    lines += [
        f"- {r.name}" + (f" (also: {', '.join(r.aliases)})" if r.aliases else "")
        for r in config.target_roles
    ]
    lines += ["", "# Preferences"]
    if p.situation:
        lines.append(f"Situation: {p.situation}")
    if p.seniority:
        lines.append(f"Seniority: {p.seniority}")
    if p.languages:
        lines.append(f"Languages: {p.languages}")
    for label, items in (
        ("Likes", p.likes),
        ("Dislikes", p.dislikes),
        ("Dealbreakers", p.dealbreakers),
    ):
        if items:
            lines.append(f"{label}:")
            lines += [f"- {item}" for item in items]
    lines += ["", "# Master CV", cv.strip()]  # cvs.ranking_cv: the other CVs follow it
    return "\n".join(lines)


def job_prompt(job: Job) -> str:
    parts = [f"Job title: {job.title}"]
    if job.company:
        parts.append(f"Employer: {job.company}")
    location = ", ".join(x for x in (job.location, job.region) if x)
    if location:
        parts.append(f"Location: {location}" + (" (remote possible)" if job.remote else ""))
    if job.employment_type:
        parts.append(f"Employment type: {job.employment_type}")
    if job.deadline:
        parts.append(f"Application deadline: {job.deadline.date().isoformat()}")
    parts += ["", "Ad text:", job.description or "(no description available)"]
    return "\n".join(parts)


def input_hash(job: Job, cv: str, config: RankingConfig, model: str) -> str:
    payload = "\x1f".join(
        [
            job.content_hash,
            hashlib.sha256(cv.encode()).hexdigest(),
            config.fingerprint(),
            model,
            PROMPT_VERSION,
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class RankReport:
    candidates: int = 0
    skipped_prefilter: int = 0
    cached: int = 0
    ranked: int = 0
    failed: int = 0
    deferred: int = 0  # over the per-run cap or budget; picked up next run
    stopped_reason: str | None = None
    stopped_on_failures: bool = False  # a failure streak ended the run: worth retrying soon


def run_ranking(
    store: Store, llm: BudgetedLLM, config: RankingConfig, cv: str, max_parallel: int = 1
) -> RankReport:
    """Rank open jobs that pass the prefilter and have no cached ranking.

    Up to `max_parallel` LLM requests run at once in worker threads. Budget checks,
    usage records and rankings are written here on the calling thread, because the
    store's SQLite connection must not be shared between threads.
    """
    report = RankReport()
    open_jobs = list(store.iter_jobs())
    selected = select_for_ranking(open_jobs, config)
    report.skipped_prefilter = len(open_jobs) - len(selected)
    report.candidates = len(selected)
    context = build_context(cv, config)

    todo: list[tuple[Job, str]] = []
    for job in selected:
        h = input_hash(job, cv, config, llm.model)
        if store.get_ranking(job.id, h) is None:
            todo.append((job, h))
        else:
            report.cached += 1
    cap = config.prefilter.max_llm_calls_per_run or len(todo)  # 0 = no limit
    if len(todo) > cap:
        report.deferred = len(todo) - cap
        report.stopped_reason = "per-run limit reached"
    queue = deque(todo[:cap])
    streak, stop_after = 0, config.prefilter.stop_after_failures  # failures in a row

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        pending: dict[Future[LLMResult], tuple[Job, str]] = {}
        while queue or pending:
            while queue and len(pending) < max_parallel:
                try:
                    llm.check()
                except BudgetExceeded as exc:
                    report.deferred += len(queue)
                    report.stopped_reason = str(exc)
                    queue.clear()
                    break
                job, h = queue.popleft()
                future = pool.submit(
                    llm.call,
                    system=SYSTEM_PROMPT,
                    context=context,
                    prompt=job_prompt(job),
                    schema=JobAssessment,
                )
                pending[future] = (job, h)
            if not pending:
                break
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                job, h = pending.pop(future)
                try:
                    result = future.result()
                except LLMError as exc:
                    if exc.usage is not None:
                        llm.record(exc.usage)
                    log.warning("Ranking %s (%s) failed: %s", job.id, job.title, exc)
                    report.failed += 1
                    streak += 1
                    if stop_after and streak >= stop_after and queue:
                        report.deferred += len(queue)
                        report.stopped_on_failures = True
                        report.stopped_reason = (
                            f"{streak} failures in a row (rate limit or outage?); "
                            "the rest wait for the next run"
                        )
                        queue.clear()
                    continue
                streak = 0
                llm.record(result.usage)
                ranking = Ranking(
                    job_id=job.id, input_hash=h, model=result.usage.model, assessment=result.parsed
                )  # type: ignore[arg-type]
                store.save_ranking(job.id, h, ranking.model_dump_json())
                _add_contacts(store, job, ranking.assessment.contact_persons)
                report.ranked += 1
                log.info("Ranked %d/%d: %s", report.ranked, min(len(todo), cap), job.title)
    return report


def ranked_jobs(store: Store, config: RankingConfig) -> list[tuple[Job, Ranking, int]]:
    """Open jobs with their current ranking and final score, best first."""
    out: list[tuple[Job, Ranking, int]] = []
    rankings = store.latest_rankings()
    jobs = {job.id: job for job in store.iter_jobs() if job.id in rankings}
    for job_id, data in rankings.items():
        job = jobs.get(job_id)
        if job is None:
            continue
        try:
            ranking = Ranking.model_validate_json(data)
        except ValueError:
            continue  # stored by an older prompt version; will be re-ranked
        out.append((job, ranking, final_score(ranking.assessment, config)))
    out.sort(key=lambda row: row[2], reverse=True)
    return out


def job_details(store: Store, job: Job, config: RankingConfig) -> dict[str, Any]:
    """A job as JSON with its latest ranking and current final score (what
    `jobsearcher show` prints and the web UI serves)."""
    out = job.model_dump(mode="json")
    latest = store.latest_ranking(job.id)
    if latest:
        data = latest[0]
        out["ranking"] = json.loads(data)
        try:
            assessment = Ranking.model_validate_json(data).assessment
            out["ranking"]["score"] = final_score(assessment, config)
        except ValueError:
            pass  # stored by an older prompt version; will be re-ranked
    return out


def _add_contacts(store: Store, job: Job, people: list[ContactPerson]) -> None:
    new = [
        Contact(name=p.name, role=p.role, email=p.email, phone=p.phone, provenance="llm:ad_text")
        for p in people
    ]
    merged = merge_contacts(job.contacts, new)
    if len(merged) != len(job.contacts):
        job.contacts = merged
        store.save_job(job)
