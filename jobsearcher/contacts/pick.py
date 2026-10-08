"""Choose the people at a company worth contacting about one job (docs/m5-contacts.md, 5c)."""

from __future__ import annotations

from pydantic import BaseModel

from jobsearcher.contacts.extract import Person, SiteFindings, guess_email
from jobsearcher.llm import BudgetedLLM, LLMError
from jobsearcher.models import Contact

MAX_PICKS = 3


class _Pick(BaseModel):
    number: int
    reason: str


class _Picks(BaseModel):
    picks: list[_Pick]


SYSTEM = """\
You help a job seeker decide whom to contact at a company. From a numbered list of \
people on the company's website, pick at most three worth writing to about the given \
job (or, for a spontaneous application, about working there): the recruiter or \
talent-acquisition person for that kind of role, or the likely hiring manager, \
i.e. the head of the team or function the role belongs to. In a small company the \
CEO is a fine choice. Skip people unrelated to hiring for it (sales, support, \
other functions). For each, give a short reason (one sentence, in English). An \
empty list is fine.
"""


def pick_people(
    llm: BudgetedLLM, findings: SiteFindings, domain: str, situation: str
) -> list[Contact]:
    """Contacts for the job described in `situation`, best first."""
    people = findings.people
    if not people:
        return []
    listing = "\n".join(
        f"{i}. {p.name}" + (f", {p.role}" if p.role else "") + (f" <{p.email}>" if p.email else "")
        for i, p in enumerate(people, start=1)
    )
    try:
        result = llm.complete(
            system=SYSTEM, prompt=f"{situation}\n\nPeople on the website:\n{listing}", schema=_Picks
        )
    except LLMError:
        return []
    answer = result.parsed
    if not isinstance(answer, _Picks):
        return []
    picked: list[Contact] = []
    seen: set[int] = set()
    for pick in answer.picks:
        if not 1 <= pick.number <= len(people) or pick.number in seen:
            continue
        seen.add(pick.number)
        picked.append(to_contact(people[pick.number - 1], findings, domain, pick.reason))
        if len(picked) == MAX_PICKS:
            break
    return picked


def to_contact(person: Person, findings: SiteFindings, domain: str, reason: str) -> Contact:
    guessed = None
    if not person.email and findings.pattern:
        guessed = guess_email(person.name, findings.pattern, domain)
    return Contact(
        name=person.name,
        role=person.role,
        email=person.email,
        phone=person.phone,
        provenance=f"company_site:{person.url}",
        url=person.url,
        note=reason.strip()[:300] or None,
        guessed_email=guessed,
    )


def job_situation(title: str, company: str, location: str | None, ad_text: str) -> str:
    where = f" in {location}" if location else ""
    return f"Job: {title} at {company}{where}.\n\nAd (start):\n{ad_text[:1500]}"


def company_situation(company: str, function: str | None) -> str:
    about = f" in {function}" if function else ""
    return f"A spontaneous application to {company}{about} (no advertised job)."
