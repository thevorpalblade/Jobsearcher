"""Draft for a job or a company: load the inputs, build the request, run the pipeline.
The CLI, the web UI's background runner and the chat all go through here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from jobsearcher import cvs as cv_files
from jobsearcher.companies.config import Company
from jobsearcher.config import Config
from jobsearcher.drafting import prompts
from jobsearcher.drafting.core import Draft, Renderer, Request, _safe, generate_draft
from jobsearcher.drafting.render import render_files
from jobsearcher.llm import BudgetedLLM, BudgetTracker, make_llm
from jobsearcher.models import Contact, Job
from jobsearcher.ranking import load_ranking_config
from jobsearcher.ranking.ranker import Ranking
from jobsearcher.store import Store

COMPANY_PREFIX = "company:"
SIGNAL_DAYS = 45
MIN_SIGNAL_RELEVANCE = 40


class DraftError(RuntimeError):
    """Something the user should be told: no CV, unknown job, ..."""


@dataclass
class Llms:
    draft: BudgetedLLM
    check: BudgetedLLM


def make_llms(config: Config, store: Store) -> Llms:
    """Drafting model (Claude Code) and the independent grounding-check model (the
    ranking model, GLM, which is free)."""
    tracker = BudgetTracker(store, config.llm)
    return Llms(make_llm(config, "drafting", tracker), make_llm(config, "ranking", tracker))


def drafts_dir(config: Config) -> Path:
    return config.data_dir / "drafts"


def _safe_key(key: str) -> str:
    """The folder name for a draft key (job id or "company:<slug>")."""
    return _safe(key)


def load_cvs(config: Config, base: str | None = None) -> list[tuple[str, str]]:
    """The CVs as [(name, text)], the base (default: the master) first."""
    files = cv_files.list_cvs(config.cv_path)
    if not files:
        raise DraftError(
            f"No CV found in {config.cv_path.parent}: upload one on the Settings page."
        )
    master = next((c for c in files if c.is_master), files[0])
    chosen = next((c for c in files if c.name == base), master) if base else master
    ordered = [chosen] + [c for c in files if c is not chosen]
    return [(c.name, c.path.read_text()) for c in ordered]


def choose_contact(job: Job) -> Contact | None:
    """A named person from the ad to address the letter to (never a generic mailbox)."""
    named = [c for c in job.contacts if c.name and c.role != "generic mailbox"]
    named.sort(key=lambda c: 0 if c.provenance.startswith("platsbanken:application") else 1)
    return named[0] if named else None


def job_request(job: Job, ranking_json: str | None, instructions: str = "") -> Request:
    assessment = None
    if ranking_json:
        try:
            assessment = Ranking.model_validate_json(ranking_json).assessment
        except ValueError:
            assessment = None  # an older prompt version's ranking: draft without it
    contact = choose_contact(job)
    identity = [job.content_hash, contact.name if contact and contact.name else ""]
    if assessment:
        identity += assessment.matched_requirements + ["|"] + assessment.missing_requirements
    return Request(
        key=job.id,
        prompt=prompts.ad_prompt(job, assessment, contact, instructions),
        identity=identity,
        instructions=instructions,
        addressed_to=contact.name if contact else None,
        sources=[job.description, job.title, job.company or ""],
    )


def company_request(
    company: Company,
    signals: list[dict[str, str]],
    target_roles: list[str],
    instructions: str = "",
) -> Request:
    if not signals:
        raise DraftError(
            f"No recent news about {company.name} to give a reason for writing; "
            "a spontaneous application needs one."
        )
    return Request(
        key=COMPANY_PREFIX + company.slug,
        prompt=prompts.spontaneous_prompt(company.name, signals, target_roles, None, instructions),
        identity=[company.name] + [s["summary"] for s in signals],
        instructions=instructions,
        sources=[s["summary"] + " " + s["title"] for s in signals],
    )


def company_signals(store: Store, company: Company) -> list[dict[str, str]]:
    since = datetime.now(UTC) - timedelta(days=SIGNAL_DAYS)
    rows = [
        r
        for r in store.signals_since(since)
        if r["company"] == company.slug
        and r["kind"] != "not_about_company"
        and r["relevance"] >= MIN_SIGNAL_RELEVANCE
    ]
    return [{"summary": r["summary"], "title": r["title"], "url": r["url"]} for r in rows[:3]]


def draft_job(
    config: Config,
    store: Store,
    job_id: str,
    instructions: str = "",
    base_cv: str | None = None,
    trigger: Literal["manual", "shortlist"] = "manual",
    force: bool = False,
    llms: Llms | None = None,
    render: Renderer | None = None,
) -> Draft:
    job = store.get_job(job_id)
    if job is None:
        raise DraftError(f"No job {job_id}")
    latest = store.latest_ranking(job_id)
    request = job_request(job, latest[0] if latest else None, instructions)
    llms = llms or make_llms(config, store)
    return generate_draft(
        store,
        request,
        load_cvs(config, base_cv),
        llms.draft,
        llms.check,
        drafts_dir(config),
        render or render_files,
        trigger=trigger,
        force=force,
    )


def draft_company(
    config: Config,
    store: Store,
    company: Company,
    instructions: str = "",
    base_cv: str | None = None,
    force: bool = False,
    llms: Llms | None = None,
    render: Renderer | None = None,
) -> Draft:
    roles = [r.name for r in load_ranking_config(config.ranking_config).target_roles]
    request = company_request(company, company_signals(store, company), roles, instructions)
    llms = llms or make_llms(config, store)
    return generate_draft(
        store,
        request,
        load_cvs(config, base_cv),
        llms.draft,
        llms.check,
        drafts_dir(config),
        render or render_files,
        force=force,
    )
