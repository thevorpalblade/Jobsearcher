"""Draft for a job or a company: load the inputs, build the request, run the pipeline.
The CLI, the web UI's background runner and the chat all go through here."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from jobsearcher import cvs as cv_files
from jobsearcher.companies.config import Company
from jobsearcher.config import DEFAULT_PROFILE, Config
from jobsearcher.drafting import prompts
from jobsearcher.drafting.core import Draft, Renderer, Request, _safe, generate_draft
from jobsearcher.drafting.render import render_files
from jobsearcher.llm import BudgetedLLM, BudgetTracker, make_llm
from jobsearcher.models import Contact, Job
from jobsearcher.ranking import load_ranking_config
from jobsearcher.ranking.ranker import Ranking
from jobsearcher.store import Store

log = logging.getLogger(__name__)

COMPANY_PREFIX = "company:"
SIGNAL_DAYS = 45
MIN_SIGNAL_RELEVANCE = 40


class DraftError(RuntimeError):
    """Something the user should be told: no CV, unknown job, ..."""


@dataclass
class Llms:
    draft: BudgetedLLM
    check: BudgetedLLM
    fallback: BudgetedLLM | None = None  # checks instead when `check` is slow or down


def make_llms(config: Config, store: Store) -> Llms:
    """Drafting model (Claude Code), the independent grounding-check model (by default
    the ranking model, GLM, which is free) and, if configured, a fallback checker."""
    tracker = BudgetTracker(store, config.llm)
    return Llms(
        make_llm(config, "drafting", tracker),
        make_llm(config, "grounding", tracker),
        make_llm(config, "grounding_fallback", tracker) if config.llm.grounding_fallback else None,
    )


def drafts_dir(config: Config) -> Path:
    """Where this profile's draft files go (data/drafts/<profile>/ once there are
    profiles; data/drafts/ for a setup without them)."""
    drafts = config.data_dir / "drafts"
    return drafts if config.profile == DEFAULT_PROFILE else drafts / config.profile


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


def _letter_contact(
    config: Config, store: Store, key: str, job: Job | None, company: Company | None = None
) -> Contact | None:
    """Whom to address the letter to: the contact the user chose, a named person from
    the ad, or one picked from the company's website. With no named person yet (and
    `contacts.before_drafting`), the website is looked up first (docs/m5-contacts.md)."""
    from jobsearcher.contacts import service as contacts

    found = contacts.load_lookup(store, key)
    has_named = job is not None and choose_contact(job) is not None
    if found is None and not has_named:
        if load_ranking_config(config.ranking_config).contacts.before_drafting:
            try:
                if job is not None:
                    found = contacts.for_job(config, store, key)
                elif company is not None:
                    found = contacts.for_company(config, store, company)
            except Exception as exc:  # no contact is fine: the letter says "Dear Hiring Manager"
                log.warning("Contact lookup before the draft failed: %s", exc)
    subject = job or Job(id=key, title="", url="")
    return contacts.letter_contact(subject, found)


def choose_contact(job: Job) -> Contact | None:
    """A named person from the ad to address the letter to (never a generic mailbox)."""
    named = [c for c in job.contacts if c.name and c.role != "generic mailbox"]
    named.sort(key=lambda c: 0 if c.provenance.startswith("platsbanken:application") else 1)
    return named[0] if named else None


def job_request(
    job: Job,
    ranking_json: str | None,
    instructions: str = "",
    contact: Contact | None = None,
) -> Request:
    """`contact`: whom to address (default: a named person from the ad)."""
    assessment = None
    if ranking_json:
        try:
            assessment = Ranking.model_validate_json(ranking_json).assessment
        except ValueError:
            assessment = None  # an older prompt version's ranking: draft without it
    contact = contact or choose_contact(job)
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
    contact: Contact | None = None,
) -> Request:
    if not signals:
        raise DraftError(
            f"No recent news about {company.name} to give a reason for writing; "
            "a spontaneous application needs one."
        )
    return Request(
        key=COMPANY_PREFIX + company.slug,
        prompt=prompts.spontaneous_prompt(
            company.name, signals, target_roles, contact, instructions
        ),
        identity=[company.name, contact.name if contact and contact.name else ""]
        + [s["summary"] for s in signals],
        instructions=instructions,
        addressed_to=contact.name if contact else None,
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
    contact = _letter_contact(config, store, job_id, job)
    request = job_request(job, latest[0] if latest else None, instructions, contact)
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
        fallback_llm=llms.fallback,
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
    signals = company_signals(store, company)
    contact = _letter_contact(config, store, COMPANY_PREFIX + company.slug, None, company)
    request = company_request(company, signals, roles, instructions, contact)
    llms = llms or make_llms(config, store)
    return generate_draft(
        store,
        request,
        load_cvs(config, base_cv),
        llms.draft,
        llms.check,
        drafts_dir(config),
        render or render_files,
        fallback_llm=llms.fallback,
        force=force,
    )
