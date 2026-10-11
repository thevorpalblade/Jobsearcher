"""Which county (län) a Swedish municipality or well-known district belongs to.

Platsbanken gives each ad its county, so "Stockholm" in `search.locations` also
matches Södertälje or Solna through "Stockholms län". Company feeds, LinkedIn and
Indeed only give a city; this fills in the county for the location filter, so the
same job passes the same way whatever the source. Covers the counties around the
default search locations; add more when searching elsewhere.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

_COUNTIES: dict[str, tuple[str, ...]] = {
    "Stockholms län": (
        "Botkyrka", "Danderyd", "Ekerö", "Haninge", "Huddinge", "Järfälla", "Lidingö",
        "Nacka", "Norrtälje", "Nykvarn", "Nynäshamn", "Salem", "Sigtuna", "Sollentuna",
        "Solna", "Stockholm", "Sundbyberg", "Södertälje", "Tyresö", "Täby",
        "Upplands Väsby", "Upplands-Bro", "Vallentuna", "Vaxholm", "Värmdö", "Österåker",
        # districts and towns often given instead of the municipality
        "Kista", "Bromma", "Arlanda", "Märsta", "Tumba", "Jakobsberg", "Kungens kurva",
        "Johanneshov", "Hägersten", "Skärholmen", "Farsta", "Vällingby", "Spånga",
        "Barkarby", "Akalla", "Frösunda", "Hagastaden", "Gustavsberg", "Åkersberga",
        "Bandhagen", "Enskede", "Enskededalen", "Älvsjö", "Sickla", "Skogås", "Ösmo",
    ),
    "Uppsala län": (
        "Enköping", "Heby", "Håbo", "Knivsta", "Tierp", "Uppsala", "Älvkarleby",
        "Östhammar", "Bålsta", "Skutskär", "Gimo",
    ),
    "Skåne län": (
        "Bjuv", "Bromölla", "Burlöv", "Båstad", "Eslöv", "Helsingborg", "Hässleholm",
        "Höganäs", "Hörby", "Höör", "Klippan", "Kristianstad", "Kävlinge", "Landskrona",
        "Lomma", "Lund", "Malmö", "Osby", "Perstorp", "Simrishamn", "Sjöbo", "Skurup",
        "Staffanstorp", "Svalöv", "Svedala", "Tomelilla", "Trelleborg", "Vellinge",
        "Ystad", "Åstorp", "Ängelholm", "Örkelljunga", "Östra Göinge", "Hyllie", "Arlöv",
    ),
    "Västra Götalands län": (
        "Ale", "Alingsås", "Bengtsfors", "Bollebygd", "Borås", "Dals-Ed", "Essunga",
        "Falköping", "Färgelanda", "Grästorp", "Gullspång", "Göteborg", "Götene",
        "Herrljunga", "Hjo", "Härryda", "Karlsborg", "Kungälv", "Lerum", "Lidköping",
        "Lilla Edet", "Lysekil", "Mariestad", "Mark", "Mellerud", "Munkedal", "Mölndal",
        "Orust", "Partille", "Skara", "Skövde", "Sotenäs", "Stenungsund", "Strömstad",
        "Svenljunga", "Tanum", "Tibro", "Tidaholm", "Tjörn", "Tranemo", "Trollhättan",
        "Töreboda", "Uddevalla", "Ulricehamn", "Vara", "Vårgårda", "Vänersborg", "Åmål",
        "Öckerö", "Torslanda", "Hisings Backa", "Floby", "Landvetter", "Lindholmen",
    ),
}  # fmt: skip

_BY_PLACE = {place.casefold(): county for county, places in _COUNTIES.items() for place in places}


def county_of(place: str | None) -> str | None:
    """ "Södertälje" -> "Stockholms län"; None when unknown."""
    if not place:
        return None
    return _BY_PLACE.get(place.strip().casefold())


# --- the search region: which country each place is in ---------------------------
#
# A profile's region (search.locations) is free text: cities, regions or countries,
# in Swedish, English or the local language. resolve() finds the country, so every
# source that covers that country searches there (regions.py), and match_terms()
# gives the names ads may use for it ("München" -> also "Munich").

_DATA = Path(__file__).resolve().parent / "data"

# Names people use for countries beyond GeoNames' English one: Swedish, local, short.
_COUNTRY_NAMES: dict[str, tuple[str, ...]] = {
    "SE": ("Sverige", "Schweden"),
    "DE": ("Tyskland", "Deutschland"),
    "IE": ("Irland", "Éire", "Eire"),
    "IT": ("Italien", "Italia"),
    "CH": ("Schweiz", "Suisse", "Svizzera"),
    "GB": ("UK", "U.K.", "Storbritannien", "Great Britain", "Britain", "Förenade kungariket"),
    "NL": ("Netherlands", "Nederländerna", "Holland", "Nederland"),
    "FR": ("Frankrike",),
    "ES": ("Spanien", "España"),
    "DK": ("Danmark",),
    "NO": ("Norge",),
    "FI": ("Suomi",),
    "AT": ("Österrike", "Österreich"),
    "BE": ("Belgien", "Belgique", "België"),
    "PL": ("Polen", "Polska"),
    "GR": ("Grekland",),
    "CZ": ("Tjeckien", "Czech Republic"),
    "HU": ("Ungern",),
    "IS": ("Island",),
    "EE": ("Estland",),
    "LV": ("Lettland",),
    "LT": ("Litauen",),
    "LU": ("Luxemburg",),
    "HR": ("Kroatien",),
    "RO": ("Rumänien",),
    "BG": ("Bulgarien",),
    "SK": ("Slovakien",),
    "SI": ("Slovenien",),
    "CY": ("Cypern",),
    "US": ("USA", "US", "Förenta staterna"),
    "CA": ("Kanada",),
    "AU": ("Australien",),
}  # fmt: skip

# Regions within a country that people name instead of a city (searched by name).
_AREAS: dict[str, tuple[str, str]] = {
    "england": ("England", "GB"),
    "scotland": ("Scotland", "GB"),
    "skottland": ("Scotland", "GB"),
    "wales": ("Wales", "GB"),
    "northern ireland": ("Northern Ireland", "GB"),
    "nordirland": ("Northern Ireland", "GB"),
    **{county.casefold(): (county, "SE") for county in _COUNTIES},
}

# Not places: whether remote jobs count is search.include_remote.
_NOT_PLACES = {"remote", "distans", "på distans", "hybrid", "anywhere", "var som helst"}

HOME = "SE"  # the home country: an empty region means all of it (Platsbanken's reach)


class Place(BaseModel):
    entry: str  # as the person wrote it
    name: str  # its English name: "Munich", "Ireland", "Scotland"
    country: str  # ISO 3166 code, e.g. "DE"
    kind: Literal["city", "area", "country"]

    @property
    def country_name(self) -> str:
        return country_name(self.country)

    def describe(self) -> str:
        """ "Munich, Germany", "Ireland" (shown next to the region in the UI)."""
        return self.name if self.kind == "country" else f"{self.name}, {self.country_name}"


@lru_cache(maxsize=1)
def _countries() -> tuple[dict[str, str], dict[str, str]]:
    """({code: English name}, {any name, casefolded: code})."""
    names: dict[str, str] = {}
    for line in (_DATA / "countries.tsv").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            code, name = line.split("\t")
            names[code] = name
    lookup = {name.casefold(): code for code, name in names.items()}
    lookup |= {code.casefold(): code for code in names if code != "IS"}  # "is" isn't Iceland
    for code, others in _COUNTRY_NAMES.items():
        lookup |= {other.casefold(): code for other in others}
    lookup["netherlands"] = "NL"  # GeoNames says "The Netherlands"
    return names, lookup


@lru_cache(maxsize=1)
def _cities() -> dict[str, list[tuple[int, int, str, str]]]:
    """{name, casefolded: [(0 for its own name/1 for another name, -population, its
    name, country), ...]}, best first: a city's own name beats another city's other
    name, then the bigger city wins ("Paris" is in France)."""
    found: dict[str, list[tuple[int, int, str, str]]] = {}
    for line in (_DATA / "cities.tsv").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, country, population, others = line.split("\t")
        entry = (-int(population), name, country)
        found.setdefault(name.casefold(), []).append((0, *entry))
        for other in others.split("|") if others else ():
            found.setdefault(other.casefold(), []).append((1, *entry))
    for options in found.values():
        options.sort()
    return found


def country_name(code: str) -> str:
    return _countries()[0].get(code, code)


def country_code(text: str | None) -> str | None:
    """ "Tyskland" / "Germany" / "DE" -> "DE"; None when it isn't a country."""
    return _countries()[1].get(text.strip().casefold()) if text else None


def resolve(entry: str) -> Place | None:
    """Which place a region entry means, or None when unknown (or not a place)."""
    text = " ".join(entry.split())
    key = text.casefold()
    if not text or key in _NOT_PLACES:
        return None
    if "," in text:  # "Bern, Switzerland": a place in a named country
        head, tail = text.rsplit(",", 1)
        code = country_code(tail)
        if code and head.strip():
            city = next(
                (c for c in _cities().get(head.strip().casefold(), []) if c[3] == code), None
            )
            return Place(
                entry=entry, name=city[2] if city else head.strip(), country=code, kind="city"
            )
    if code := country_code(text):
        return Place(entry=entry, name=country_name(code), country=code, kind="country")
    if key in _AREAS:
        name, code = _AREAS[key]
        return Place(entry=entry, name=name, country=code, kind="area")
    if county_of(text):  # a Swedish municipality or district (Swedish spelling)
        return Place(entry=entry, name=text, country="SE", kind="city")
    if options := _cities().get(key):
        _, _, name, code = options[0]
        return Place(entry=entry, name=name, country=code, kind="city")
    return None


@lru_cache(maxsize=1024)  # the filter asks for every job
def match_terms(entry: str) -> tuple[str, ...]:
    """The names a job's location may use for a region entry: as written, plus its
    English name ("München" -> "Munich", "Tyskland" -> "Germany")."""
    place = resolve(entry)
    written = entry.split(",")[0].strip() if place and place.kind == "city" else entry.strip()
    terms = [written]
    if place and place.name.casefold() != written.casefold():
        terms.append(place.name)
    return tuple(terms)


def fold(text: str) -> str:
    """Lower case without accents, for comparing place names: "Zürich" ~ "Zurich"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))
