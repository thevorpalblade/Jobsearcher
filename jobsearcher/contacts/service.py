"""Find contact people for a job or a company (docs/m5-contacts.md): the employer's
website (cached per company), the people on it (cached per domain, shared by every
profile), and the ones picked for this job (the profile's own)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from jobsearcher.companies.config import Company, load_companies
from jobsearcher.companies.http import PoliteClient
from jobsearcher.config import Config
from jobsearcher.contacts.extract import Person, SiteFindings, site_findings
from jobsearcher.contacts.pick import company_situation, job_situation, pick_people
from jobsearcher.contacts.site import company_key, contact_pages, find_site, verify
from jobsearcher.llm import BudgetedLLM, BudgetTracker, LLMError, make_llm
from jobsearcher.models import Contact, Job
from jobsearcher.store import Store

log = logging.getLogger(__name__)

SITE_FOUND_TTL = timedelta(days=90)
SITE_MISSING_TTL = timedelta(days=7)  # try again sooner when nothing was found
CONTACTS_TTL = timedelta(days=30)
COMPANY_PREFIX = "company:"


@dataclass
class Lookup:
    """What a lookup found for a job or company (stored per profile)."""

    contacts: list[Contact] = field(default_factory=list)
    domain: str | None = None
    site_source: str | None = None
    mailboxes: list[str] = field(default_factory=list)
    pattern_url: str | None = None
    error: str | None = None
    chosen: str | None = None  # contact_key() of the one to address the letter to
    checked_at: datetime | None = None

    def to_json(self) -> str:
        data = {
            "contacts": [c.model_dump(exclude_none=True) for c in self.contacts],
            "domain": self.domain,
            "site_source": self.site_source,
            "mailboxes": self.mailboxes,
            "pattern_url": self.pattern_url,
            "error": self.error,
        }
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_row(cls, row) -> Lookup:  # type: ignore[no-untyped-def]
        data = json.loads(row["data"] or "{}")
        return cls(
            contacts=[Contact.model_validate(c) for c in data.get("contacts", [])],
            domain=data.get("domain"),
            site_source=data.get("site_source"),
            mailboxes=data.get("mailboxes", []),
            pattern_url=data.get("pattern_url"),
            error=data.get("error"),
            chosen=row["chosen"],
            checked_at=datetime.fromisoformat(row["created_at"]),
        )


def contact_key(contact: Contact) -> str:
    return "|".join(contact.key())


def load_lookup(store: Store, key: str) -> Lookup | None:
    row = store.job_contacts(key)
    return Lookup.from_row(row) if row is not None else None


def _fresh(when: str, ttl: timedelta) -> bool:
    return datetime.now(UTC) - datetime.fromisoformat(when) < ttl


def _findings_from_json(text: str) -> SiteFindings:
    data = json.loads(text)
    return SiteFindings(
        people=[Person(**p) for p in data.get("people", [])],
        mailboxes=data.get("mailboxes", []),
        pattern=data.get("pattern"),
        pattern_url=data.get("pattern_url"),
    )


def _findings_to_json(findings: SiteFindings) -> str:
    return json.dumps(
        {
            "people": [p.as_dict() for p in findings.people],
            "mailboxes": findings.mailboxes,
            "pattern": findings.pattern,
            "pattern_url": findings.pattern_url,
        },
        ensure_ascii=False,
    )


class _Domain(BaseModel):
    domain: str | None


def domain_guesser(llm: BudgetedLLM):  # type: ignore[no-untyped-def]
    def guess(company: str) -> str | None:
        try:
            result = llm.complete(
                system="Answer with the domain of the employer's official website (e.g. "
                "acme.se), or null if you don't know it. Never guess a domain you're unsure of.",
                prompt=f"Employer in Sweden: {company}",
                schema=_Domain,
            )
        except LLMError:
            return None
        return result.parsed.domain if isinstance(result.parsed, _Domain) else None

    return guess


@dataclass
class Clients:
    http: PoliteClient
    llm: BudgetedLLM | None  # None: no model step (structured data and links only)


def make_clients(config: Config, store: Store) -> Clients:
    try:
        llm = make_llm(config, "ranking", BudgetTracker(store, config.llm))
        llm.purpose = "contacts"  # its own line in the usage summary
    except LLMError as exc:
        log.warning("Contacts without a model: %s", exc)
        llm = None
    return Clients(PoliteClient.from_config(config), llm)


def known_websites(config: Config) -> dict[str, str]:
    """company key -> website, from this profile's companies.yaml."""
    if not config.companies_config.is_file():
        return {}
    return {
        company_key(c.name): c.website for c in load_companies(config.companies_config) if c.website
    }


def resolve_site(
    store: Store, job: Job, known: dict[str, str], clients: Clients, force: bool = False
) -> tuple[str | None, str | None, tuple[str, str] | None]:
    """(domain, where it came from, (home URL, HTML) if fetched now)."""
    key = company_key(job.company or "")
    row = store.company_site(key)
    if row is not None and not force:
        manual = row["source"] == "manual"
        ttl = SITE_FOUND_TTL if row["domain"] else SITE_MISSING_TTL
        if manual or _fresh(row["checked_at"], ttl):
            return row["domain"], row["source"], None
    guess = domain_guesser(clients.llm) if clients.llm is not None else None
    found = find_site(job, known, clients.http, guess)
    if found is None:
        store.save_company_site(key, None, "not found")
        return None, None, None
    domain, source, home_url, home_html = found
    store.save_company_site(key, domain, source)
    return domain, source, (home_url, home_html)


def set_site(store: Store, company: str, domain: str | None) -> None:
    """The user's correction of a company's website (kept until changed again)."""
    store.save_company_site(company_key(company), domain or None, "manual")


def findings_for(
    store: Store,
    company: str,
    domain: str,
    clients: Clients,
    home: tuple[str, str] | None,
    force: bool = False,
) -> SiteFindings | None:
    row = store.company_contacts(domain)
    if row is not None and not force and _fresh(row["fetched_at"], CONTACTS_TTL):
        return _findings_from_json(row["data"])
    if home is None:
        home = verify(domain, company, clients.http)
        if home is None:
            return None
    pages = contact_pages(*home, clients.http)
    findings = site_findings(clients.llm, company, domain, pages)
    store.save_company_contacts(domain, _findings_to_json(findings))
    return findings


def lookup(
    config: Config,
    store: Store,
    key: str,
    subject: Job,
    situation: str,
    clients: Clients | None = None,
    force: bool = False,
) -> Lookup:
    """Find, pick and save the contacts for `key` (a job id or company:<slug>)."""
    clients = clients or make_clients(config, store)
    previous = load_lookup(store, key)
    result = Lookup(chosen=previous.chosen if previous else None)
    domain, source, home = resolve_site(store, subject, known_websites(config), clients, force)
    result.domain, result.site_source = domain, source
    if domain is None:
        result.error = "Couldn't find the company's website. Set it below if you know it."
    else:
        findings = findings_for(store, subject.company or "", domain, clients, home, force)
        if findings is None:
            result.error = f"{domain} didn't answer, or doesn't mention {subject.company}."
        else:
            result.mailboxes, result.pattern_url = findings.mailboxes, findings.pattern_url
            if not findings.people:
                result.error = f"No named people on {domain}'s contact and about pages."
            elif clients.llm is None:
                result.error = "No model to pick people with (check Settings → Models)."
            else:
                result.contacts = pick_people(clients.llm, findings, domain, situation)
                if not result.contacts:
                    result.error = f"Nobody on {domain} looks right for this; see the search links."
    store.save_job_contacts(key, result.to_json())
    return result


def for_job(
    config: Config,
    store: Store,
    job_id: str,
    force: bool = False,
    clients: Clients | None = None,
) -> Lookup:
    job = store.get_job(job_id)
    if job is None:
        raise ValueError(f"No job {job_id}")
    situation = job_situation(job.title, job.company or "", job.location, job.description)
    return lookup(config, store, job_id, job, situation, clients, force)


def due_for_lookup(store: Store, settings) -> list[Job]:  # type: ignore[no-untyped-def]
    """This profile's jobs worth an automatic lookup: scoring at least
    settings.auto_min_score, or shortlisted, with no named person in the ad and no
    lookup yet. Best first."""
    from jobsearcher.models import ApplicationState
    from jobsearcher.ranking import ranked_jobs
    from jobsearcher.ranking.config import RankingConfig

    assert isinstance(settings, RankingConfig)
    done = store.all_job_contacts()
    apps = store.applications()
    shortlisted = {k for k, a in apps.items() if a.state == ApplicationState.SHORTLISTED}
    due: dict[str, Job] = {}
    for job, _, score in ranked_jobs(store, settings):
        if score >= settings.contacts.auto_min_score or job.id in shortlisted:
            due[job.id] = job
    for job_id in shortlisted - set(due):  # shortlisted but not ranked (yet)
        job = store.get_job(job_id)
        if job is not None:
            due[job_id] = job
    return [
        job
        for job in due.values()
        if job.company and job.id not in done and letter_contact(job, None) is None
    ]


def for_company(
    config: Config, store: Store, company: Company, function: str | None = None, force: bool = False
) -> Lookup:
    subject = Job(
        id=COMPANY_PREFIX + company.slug, title="", company=company.name, url=company.website or ""
    )
    if company.website:
        subject.company_url = company.website
    situation = company_situation(company.name, function)
    return lookup(config, store, COMPANY_PREFIX + company.slug, subject, situation, force=force)


def letter_contact(job: Job, found: Lookup | None) -> Contact | None:
    """Whom to address the letter to: the contact the user chose, else a named person
    from the ad, else the first person picked from the website."""
    candidates = list(job.contacts) + (found.contacts if found else [])
    named = [c for c in candidates if c.name and c.role != "generic mailbox"]
    if found and found.chosen:
        for contact in named:
            if contact_key(contact) == found.chosen:
                return contact
    from_ad = [c for c in named if not c.provenance.startswith("company_site:")]
    from_ad.sort(key=lambda c: 0 if c.provenance.startswith("platsbanken:application") else 1)
    return (from_ad or named or [None])[0]
