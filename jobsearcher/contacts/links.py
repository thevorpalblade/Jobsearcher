"""Search links for finding a contact person by hand (docs/m5-contacts.md, 5a).

Nothing is fetched or stored: they're links the person opens themselves. LinkedIn is
never scraped (it's against their terms), so its people search is only a link.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote_plus

_LEGAL = re.compile(
    r"\s*(?:\(publ\)|\bAB\b|\bAktiebolag\b|\bSverige\b|\bSweden\b|\bNordics?\b|[®™])", re.I
)
RECRUITER_WORDS = '(rekryterare OR recruiter OR "talent acquisition" OR HR)'


@dataclass(frozen=True)
class SearchLink:
    label: str
    url: str


def plain_company(name: str) -> str:
    """The company's name without legal form or country, as people write it."""
    return " ".join(_LEGAL.sub(" ", name).split()) or name.strip()


def linkedin_people(query: str) -> str:
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(query)}"


def google(query: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}"


def search_links(
    company: str | None, role: str | None = None, domain: str | None = None
) -> list[SearchLink]:
    """Searches for the company's recruiters and for the head of the function `role`
    belongs to (e.g. the job's matched target role)."""
    if not company:
        return []
    name = plain_company(company)
    links = [
        SearchLink("Recruiters on LinkedIn", linkedin_people(f'"{name}" {RECRUITER_WORDS}')),
        SearchLink(
            "Recruiters via Google", google(f'site:linkedin.com/in "{name}" {RECRUITER_WORDS}')
        ),
    ]
    if role:
        links.append(
            SearchLink(
                f"Heads of {role} on LinkedIn",
                linkedin_people(f'"{name}" {role} (chef OR head OR manager OR director)'),
            )
        )
    if domain:
        links.append(
            SearchLink(
                f"Contact pages on {domain}",
                google(f"site:{domain} (kontakt OR contact OR team OR ledning OR management)"),
            )
        )
    return links
