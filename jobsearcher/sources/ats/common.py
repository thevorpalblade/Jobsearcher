"""Helpers shared by the ATS adapters."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from typing import Any, Protocol

from jobsearcher.companies.config import Company, slugify
from jobsearcher.models import Job

# Called by adapters before an expensive per-job request (e.g. fetching the full ad):
# returns False for jobs the pipeline would drop anyway (wrong place, excluded words).
Wanted = Callable[[Job], bool]


class AtsFetcher(Protocol):
    def __call__(
        self, client: AtsClient, ref: str, company: Company, wanted: Wanted
    ) -> Iterator[Job]: ...


class AtsClient(Protocol):
    def get_json(
        self, url: str, params: dict | None = None, headers: dict[str, str] | None = None
    ) -> object: ...

    def get_text(self, url: str, params: dict | None = None) -> str: ...

    def post_json(self, url: str, body: Any, headers: dict[str, str] | None = None) -> Any: ...


def feed_source(ats_type: str, ref: str) -> str:
    """Source name for jobs from one ATS feed, e.g. "varbi:sll". Named after the feed,
    not the company, so companies sharing a feed (a region and its hospitals) produce
    the same (source, source_id) pairs and their jobs merge instead of duplicating."""
    return f"{ats_type}:{slugify(ref.split('://')[-1])}"


_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style"}:
            self.skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1
        elif tag in _BLOCK_TAGS and tag != "li":  # an item's start already broke the line
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def html_to_text(markup: str | None) -> str:
    """Readable plain text from an HTML job description (ranking reads plain text)."""
    if not markup:
        return ""
    parser = _TextExtractor()
    parser.feed(html.unescape(markup) if "&lt;" in markup else markup)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Swedish cities that ATS postings commonly name without a country.
SWEDISH_CITIES = (
    "stockholm", "göteborg", "gothenburg", "malmö", "malmo", "lund", "uppsala", "solna",
    "sundbyberg", "kista", "södertälje", "linköping", "norrköping", "västerås", "örebro",
    "helsingborg", "jönköping", "umeå", "luleå", "karlstad", "växjö", "gävle", "sundsvall",
)  # fmt: skip


# English place names some sources use, mapped to the Swedish ones Platsbanken uses,
# so dedupe can merge the same job across sources.
_CITY_NAMES = {
    "gothenburg": "Göteborg",
    "goteborg": "Göteborg",
    "malmo": "Malmö",
    "linkoping": "Linköping",
    "norrkoping": "Norrköping",
    "vasteras": "Västerås",
    "orebro": "Örebro",
    "jonkoping": "Jönköping",
    "umea": "Umeå",
    "lulea": "Luleå",
    "vaxjo": "Växjö",
    "gavle": "Gävle",
    "sodertalje": "Södertälje",
}


def swedish_name(city: str) -> str:
    """ "Gothenburg" -> "Göteborg"; other names unchanged."""
    return _CITY_NAMES.get(city.strip().casefold(), city.strip())


def swedish_city(text: str | None) -> str | None:
    """The Swedish city named first in `text` (Swedish spelling), else None."""
    if not text:
        return None
    lowered = text.lower()
    found = [(i, city) for city in SWEDISH_CITIES if (i := lowered.find(city)) >= 0]
    if not found:
        return None
    i, city = min(found)  # the first one named, e.g. "Solna, Göteborg" -> Solna
    return swedish_name(text[i : i + len(city)])


_SWEDEN = re.compile(r"\b(sweden|sverige|schweden|suède)\b", re.IGNORECASE)


def is_sweden(country: str | None, *texts: str | None) -> bool | None:
    """True/False when the country is known (from a code/name or the location text),
    None when it can't be told."""
    if country:
        c = country.strip().lower()
        return c in {"se", "swe", "sweden", "sverige"}
    if any(t and _SWEDEN.search(t) for t in texts):
        return True
    return None
