"""People named on a company's own pages (docs/m5-contacts.md, 5b).

Structured data first (schema.org Person, mailto:/tel: links), then one cheap model
call over the pages' text. A model answer is only kept if the name (and any email or
phone) is literally on the page it cites: nothing is invented.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass

from pydantic import BaseModel

from jobsearcher.contacts import EMAIL_RE, PHONE_RE, is_generic_email
from jobsearcher.contacts.site import page_text
from jobsearcher.llm import BudgetedLLM, LLMError

PAGE_CHARS = 6000  # of each page's text sent to the model

_LD_JSON = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I
)


@dataclass
class Person:
    name: str
    role: str | None
    email: str | None
    phone: str | None
    url: str  # the page they're on
    source: str  # "jsonld" or "model"

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass
class SiteFindings:
    people: list[Person]
    mailboxes: list[str]  # addresses that aren't one person's, e.g. rekrytering@
    pattern: str | None  # e.g. "{first}.{last}" for guessing addresses
    pattern_url: str | None  # where a real address in that pattern was seen


# Every field is required (nullable), as strict structured output expects.
class _Named(BaseModel):
    name: str
    role: str | None
    email: str | None
    phone: str | None
    page: int


class _NamedPeople(BaseModel):
    people: list[_Named]


SYSTEM = """\
You read pages from a company's own website and list the people named on them who \
work for the company: their name, role or title, email and phone, copied exactly as \
written on the page, and the number of the page they're on. Only people whose name is \
on the page; never complete a missing email or phone, leave it null. Skip customers, \
quoted outsiders and authors of articles about other companies. An empty list is fine.
"""


def _fold(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(plain.lower().split())


def _digits(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def jsonld_people(html_text: str, url: str) -> list[Person]:
    found: list[Person] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Person" in kinds and isinstance(node.get("name"), str):
                found.append(
                    Person(
                        name=" ".join(node["name"].split()),
                        role=node.get("jobTitle")
                        if isinstance(node.get("jobTitle"), str)
                        else None,
                        email=str(node["email"]).removeprefix("mailto:")
                        if node.get("email")
                        else None,
                        phone=str(node["telephone"]) if node.get("telephone") else None,
                        url=url,
                        source="jsonld",
                    )
                )
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

    for block in _LD_JSON.findall(html_text):
        try:
            walk(json.loads(block.strip()))
        except json.JSONDecodeError:
            continue
    return found


def emails_on(html_text: str) -> list[str]:
    """Addresses on a page: mailto: links and anything shaped like one in the text."""
    raw = re.findall(r"mailto:([^\"'?>\s]+)", html_text, re.I) + EMAIL_RE.findall(
        page_text(html_text)
    )
    return list(dict.fromkeys(e.strip(".").lower() for e in raw))


def literally_on_page(person: _Named, text: str, raw: str) -> bool:
    """Is the model's answer really on the page it cites?"""
    folded = _fold(text)
    if not person.name or _fold(person.name) not in folded:
        return False
    if person.email and person.email.lower() not in (text + raw).lower():
        return False
    if person.phone and _digits(person.phone) not in _digits(text):
        return False
    return True


def model_people(llm: BudgetedLLM, company: str, pages: list[tuple[str, str]]) -> list[Person]:
    texts = [page_text(html_text) for _, html_text in pages]
    prompt = f"Company: {company}\n\n" + "\n\n".join(
        f"## Page {i}: {url}\n{text[:PAGE_CHARS]}"
        for i, ((url, _), text) in enumerate(zip(pages, texts, strict=True), start=1)
    )
    try:
        result = llm.complete(system=SYSTEM, prompt=prompt, schema=_NamedPeople)
    except LLMError:
        return []
    answer = result.parsed
    if not isinstance(answer, _NamedPeople):
        return []
    people = []
    for named in answer.people:
        if not 1 <= named.page <= len(pages):
            continue
        url, raw = pages[named.page - 1]
        if literally_on_page(named, texts[named.page - 1], raw):
            people.append(
                Person(
                    name=" ".join(named.name.split()),
                    role=named.role,
                    email=named.email.lower() if named.email else None,
                    phone=named.phone,
                    url=url,
                    source="model",
                )
            )
    return people


# Local parts, as patterns over a person's first and last name.
PATTERNS = ("{first}.{last}", "{first}{last}", "{f}.{last}", "{f}{last}", "{first}_{last}",
            "{first}-{last}", "{first}")  # fmt: skip


def _name_parts(name: str) -> tuple[str, str] | None:
    words = [re.sub(r"[^a-z]", "", w) for w in _fold(name).split()]
    words = [w for w in words if w]
    return (words[0], words[-1]) if len(words) >= 2 else None


def local_part(pattern: str, name: str) -> str | None:
    parts = _name_parts(name)
    if parts is None:
        return None
    first, last = parts
    return pattern.format(first=first, last=last, f=first[0])


def find_pattern(people: list[Person], domain: str) -> tuple[str, str] | None:
    """(pattern, page) if a named person's real address on the company's domain fits
    one: at least one real example is required before anything is guessed."""
    for person in people:
        if not person.email or not person.email.endswith("@" + domain):
            continue
        local = person.email.split("@", 1)[0]
        for pattern in PATTERNS:
            if local_part(pattern, person.name) == local:
                return pattern, person.url
    return None


def guess_email(name: str, pattern: str, domain: str) -> str | None:
    local = local_part(pattern, name)
    return f"{local}@{domain}" if local else None


def site_findings(
    llm: BudgetedLLM | None, company: str, domain: str, pages: list[tuple[str, str]]
) -> SiteFindings:
    people: dict[str, Person] = {}
    mailboxes: list[str] = []
    for url, html_text in pages:
        for person in jsonld_people(html_text, url):
            people.setdefault(_fold(person.name), person)
        for email in emails_on(html_text):
            if is_generic_email(email) and email not in mailboxes:
                mailboxes.append(email)
    if llm is not None:
        for person in model_people(llm, company, pages):
            people.setdefault(_fold(person.name), person)
    listed = list(people.values())
    for person in listed:  # a phone number shaped like one, kept as written
        if person.phone and not PHONE_RE.search(person.phone) and len(_digits(person.phone)) < 7:
            person.phone = None
    found = find_pattern(listed, domain)
    return SiteFindings(
        people=listed,
        mailboxes=mailboxes,
        pattern=found[0] if found else None,
        pattern_url=found[1] if found else None,
    )
