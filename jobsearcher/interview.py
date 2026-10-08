"""The preferences interview (web UI tab): a model interviews the candidate, then
proposes their job-search settings, which they review before anything is saved.

Plain model calls with no tools (unlike the Claude Code chat), on the profile's own
drafting model and budget, so it's safe for every user, also on the internet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from jobsearcher.companies.config import CompaniesConfig, slugify
from jobsearcher.config import Config, load_profile_settings
from jobsearcher.cvs import ranking_cv
from jobsearcher.llm import BudgetedLLM
from jobsearcher.ranking import load_ranking_config
from jobsearcher.store import Store

TOPICS = (
    "situation",
    "roles",
    "region",
    "seniority",
    "languages",
    "likes",
    "dislikes",
    "dealbreakers",
    "companies",
)
MAX_ANSWER = 4000  # characters per answer

SYSTEM = """\
You are interviewing {name}, a job seeker in Sweden, to set up their job search in \
Jobsearcher, a tool that finds job ads, ranks them against their CV and drafts \
applications. Interview in {language}. Ask ONE short question at a time (a sentence \
or two; at most a short list of options), warm and to the point. Build on their CV and \
current settings below: suggest, then let them confirm or correct, rather than asking \
for things you can see. Never invent facts about them.

Cover these topics, in a natural order:
1. situation: where they are now and what they want next (and anything the CV doesn't \
show that matters, e.g. where they live, notice period, work permit).
2. roles: suggest 3-5 target roles that fit their CV; agree on the ones to search for. \
For each, agree the job titles employers use in Swedish and English (these become \
search words, so ask about common variants).
3. region: the cities or regions they'd work in, how far they'd commute, remote or \
hybrid.
4. seniority: the level they're aiming for.
5. languages: the languages they can work in, and how well.
6. likes: what they want in a job and an employer.
7. dislikes: what they'd rather avoid.
8. dealbreakers: what rules a job out entirely.
9. companies: employers they'd especially like to work for. Then ask whether they'd \
like you to suggest more companies like those; if yes, suggest 4-6 real companies in \
their region with a short reason each, and ask which to add.

When every topic is covered, give a short summary and ask them to press "Make my \
settings" (set done to true then). List in topics_covered the topics already covered, \
using the names above.
"""

PROPOSAL_SYSTEM = """\
From this interview, fill in the candidate's job-search settings. Use only what the \
candidate said or confirmed; leave things out rather than guessing. Rules:
- target_roles: the roles they agreed on, each named in English, with aliases: every \
job title variant for it they agreed on, in Swedish and English (lower case is fine).
- situation, seniority, languages: short sentences in English, as a brief for a \
recruiter.
- likes, dislikes, dealbreakers: short phrases in English.
- locations: the places they'd work, as written in Swedish job ads (e.g. "Stockholm", \
"Göteborg", "Uppsala"); include_remote: whether remote jobs elsewhere are fine.
- keywords: extra search words beyond the role titles (often none). exclude_keywords: \
words that rule an ad out (e.g. "konsultuppdrag"), only if they asked for that.
- companies: the employers they named (suggested false) and the suggestions they \
accepted (suggested true), with the website if you know it; never ones they declined.
"""


class Turn(BaseModel):
    message: str
    done: bool
    topics_covered: list[str]


class ProposedRole(BaseModel):
    name: str
    aliases: list[str]


class ProposedCompany(BaseModel):
    name: str
    website: str | None
    reason: str | None
    suggested: bool


class Proposal(BaseModel):
    target_roles: list[ProposedRole]
    situation: str
    seniority: str
    languages: str
    likes: list[str]
    dislikes: list[str]
    dealbreakers: list[str]
    locations: list[str]
    include_remote: bool
    keywords: list[str]
    exclude_keywords: list[str]
    companies: list[ProposedCompany]


@dataclass
class Interview:
    language: str = "English"
    messages: list[dict[str, str]] = field(default_factory=list)  # role: interviewer|candidate
    covered: list[str] = field(default_factory=list)
    done: bool = False
    proposal: Proposal | None = None
    updated_at: datetime | None = None
    # When its settings were saved: the interview is complete (the dashboard stops
    # nudging, and calibration opens once jobs are ranked with them).
    applied_at: datetime | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "language": self.language,
                "messages": self.messages,
                "covered": self.covered,
                "done": self.done,
                "proposal": self.proposal.model_dump() if self.proposal else None,
                "applied_at": self.applied_at.isoformat() if self.applied_at else None,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_row(cls, row: Any) -> Interview:
        data = json.loads(row["data"])
        proposal = data.get("proposal")
        return cls(
            language=data.get("language", "English"),
            messages=data.get("messages", []),
            covered=data.get("covered", []),
            done=data.get("done", False),
            proposal=Proposal.model_validate(proposal) if proposal else None,
            updated_at=datetime.fromisoformat(row["updated_at"]),
            applied_at=datetime.fromisoformat(data["applied_at"])
            if data.get("applied_at")
            else None,
        )

    def transcript(self) -> str:
        who = {"interviewer": "Interviewer", "candidate": "Candidate"}
        return "\n\n".join(f"{who[m['role']]}: {m['text']}" for m in self.messages)


def mark_applied(store: Store) -> None:
    interview = load(store)
    if interview is not None:
        interview.applied_at = now()
        store.save_interview(interview.to_json())


def completed(store: Store) -> datetime | None:
    """When this profile's interview settings were saved, if ever."""
    interview = load(store)
    return interview.applied_at if interview else None


def load(store: Store) -> Interview | None:
    row = store.interview()
    return Interview.from_row(row) if row is not None else None


def current_settings(config: Config) -> str:
    """The profile's present search settings, so the interview builds on them."""
    rc = load_ranking_config(config.ranking_config)
    settings = load_profile_settings(config.profile_file) if config.profile_file else None
    search = settings.search if settings else config.search
    companies: list[str] = []
    if config.companies_config.is_file():
        raw = yaml.safe_load(config.companies_config.read_text()) or {}
        companies = [c.get("name", "") for c in raw.get("companies", []) if isinstance(c, dict)]
    data = {
        "target_roles": [{"name": r.name, "aliases": r.aliases} for r in rc.target_roles],
        "preferences": rc.preferences.model_dump(),
        "search": {
            "locations": search.locations,
            "include_remote": search.include_remote,
            "keywords": search.keywords,
            "exclude_keywords": search.exclude_keywords,
        },
        "companies": companies[:60],
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _context(config: Config) -> str:
    cv = ranking_cv(config.cv_path) or "(no CV uploaded yet)"
    return f"# Their CVs\n{cv}\n\n# Their current settings\n{current_settings(config)}"


def _system(config: Config, interview: Interview) -> str:
    return SYSTEM.format(name=config.web.user_name or "the candidate", language=interview.language)


def start(config: Config, store: Store, llm: BudgetedLLM, language: str) -> Interview:
    """A new interview (replacing any earlier one) and its first question. An earlier
    interview's completion is kept: its settings are still the ones in use."""
    earlier = load(store)
    interview = Interview(language=language, applied_at=earlier.applied_at if earlier else None)
    _ask(
        config,
        llm,
        interview,
        "Start the interview: greet them briefly, then ask the first question.",
    )
    store.save_interview(interview.to_json())
    return interview


def answer(config: Config, store: Store, llm: BudgetedLLM, text: str) -> Interview:
    interview = load(store)
    if interview is None:
        raise ValueError("Start the interview first.")
    text = text.strip()[:MAX_ANSWER]
    if not text:
        raise ValueError("Write an answer first.")
    interview.messages.append({"role": "candidate", "text": text})
    interview.proposal = None  # the answers changed: propose again
    _ask(config, llm, interview, "Continue the interview with your next question (or wrap up).")
    store.save_interview(interview.to_json())
    return interview


def _ask(config: Config, llm: BudgetedLLM, interview: Interview, instruction: str) -> None:
    prompt = f"Conversation so far:\n\n{interview.transcript() or '(nothing yet)'}\n\n{instruction}"
    result = llm.complete(
        system=_system(config, interview), context=_context(config), prompt=prompt, schema=Turn
    )
    turn = result.parsed
    if not isinstance(turn, Turn):
        raise ValueError("The model's answer didn't fit the interview format; try again.")
    interview.messages.append({"role": "interviewer", "text": turn.message.strip()})
    interview.covered = [t for t in TOPICS if t in turn.topics_covered]
    interview.done = turn.done


def propose(config: Config, store: Store, llm: BudgetedLLM) -> Interview:
    interview = load(store)
    if interview is None or not any(m["role"] == "candidate" for m in interview.messages):
        raise ValueError("Answer a few questions first.")
    result = llm.complete(
        system=PROPOSAL_SYSTEM,
        context=_context(config),
        prompt=f"The interview:\n\n{interview.transcript()}\n\nFill in the settings.",
        schema=Proposal,
    )
    if not isinstance(result.parsed, Proposal):
        raise ValueError("The model's settings didn't fit the format; try again.")
    interview.proposal = result.parsed
    store.save_interview(interview.to_json())
    return interview


# --- saving a reviewed proposal ----------------------------------------------------


def ranking_data(old_text: str, proposal: Proposal) -> dict[str, Any]:
    """ranking.yaml's target roles and preferences from the proposal. A role that
    already exists keeps its other settings (e.g. occupation filters)."""
    old = yaml.safe_load(old_text) if old_text.strip() else {}
    old_roles = {
        str(r.get("name", "")).casefold(): r
        for r in (old or {}).get("target_roles") or []
        if isinstance(r, dict)
    }
    roles = []
    for role in proposal.target_roles:
        merged = dict(old_roles.get(role.name.casefold(), {}))
        merged.update({"name": role.name, "aliases": _unique(role.aliases)})
        roles.append(merged)
    preferences = {
        "situation": proposal.situation,
        "seniority": proposal.seniority,
        "languages": proposal.languages,
        "likes": proposal.likes,
        "dislikes": proposal.dislikes,
        "dealbreakers": proposal.dealbreakers,
    }
    return {"target_roles": roles, "preferences": preferences}


def profile_data(proposal: Proposal) -> dict[str, Any]:
    return {
        "search": {
            "locations": _unique(proposal.locations),
            "include_remote": proposal.include_remote,
            "keywords": _unique(proposal.keywords),
            "exclude_keywords": _unique(proposal.exclude_keywords),
        }
    }


def companies_text(old_text: str, proposal: Proposal) -> tuple[str, list[str]]:
    """companies.yaml with the proposal's new companies appended (existing ones, and
    their comments, are left alone), and the names added."""
    from jobsearcher.settings import merge_yaml

    old = (yaml.safe_load(old_text) if old_text.strip() else None) or {}
    current = [c for c in old.get("companies") or [] if isinstance(c, dict) and c.get("name")]
    existing = {slugify(c["name"]) for c in current}
    added: list[dict[str, Any]] = []
    for company in proposal.companies:
        name = company.name.strip()
        if not name or slugify(name) in existing:
            continue
        existing.add(slugify(name))
        entry: dict[str, Any] = {"name": name, "source": "interview"}
        if company.website:
            entry["website"] = company.website.strip()
        if company.suggested:
            entry["tags"] = ["suggested"]
        added.append(entry)
    if not added:
        return old_text, []
    keep = [{"name": c["name"]} for c in current]  # by name: unchanged
    text = merge_yaml(old_text or "companies: []\n", {"companies": keep + added})
    CompaniesConfig.model_validate(yaml.safe_load(text))  # still a valid file
    return text, [c["name"] for c in added]


def _unique(items: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for item in items:
        item = " ".join(str(item).split())
        if item and item.casefold() not in seen:
            seen[item.casefold()] = item
    return list(seen.values())


def now() -> datetime:
    return datetime.now(UTC)


def path_text(path: Path | None) -> str:
    return path.read_text() if path is not None and path.is_file() else ""
