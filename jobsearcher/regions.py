"""Where each source searches, from the profiles' regions (search.locations).

Every place is resolved to its country (places.resolve), and each source searches the
countries it covers: Platsbanken and JobTech Links Sweden, environmentjob the UK,
eurojobs by country, LinkedIn and Indeed by city (or the whole country when a profile
names it, or more than MAX_CITIES of its cities). One plan serves every profile.

A profile's first country (its first place's) is searched with all its keywords. Its
other places get only its main keywords (the role names), and those searches run
with the slow sources after the rest of a run is ranked: on the rate-limited sites,
every place multiplies the requests.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from jobsearcher.config import Config
from jobsearcher.places import HOME, Place, country_name, resolve

# More of a profile's places than this in one country: search the whole country
# (one search instead of one per city).
MAX_CITIES = 3


class SearchArea(BaseModel):
    model_config = ConfigDict(frozen=True)  # a dict key

    country: str  # ISO 3166 code
    place: str | None = None  # a city or region in it (English name); None: all of it

    def label(self) -> str:
        """ "Dublin, Ireland" or "Ireland" (also LinkedIn's location query)."""
        name = country_name(self.country)
        return f"{self.place}, {name}" if self.place else name


class Target(BaseModel):
    area: SearchArea
    keywords: list[str]
    primary: bool  # some profile's first country: searched in the main run

    def wants(self, keyword: str) -> bool:
        return keyword.casefold() in {k.casefold() for k in self.keywords}


class SearchPlan(BaseModel):
    targets: list[Target] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)  # region entries with no known place

    @property
    def countries(self) -> set[str]:
        """The countries searched (no targets, e.g. no keywords yet: Sweden)."""
        return {t.area.country for t in self.targets} or {HOME}

    def keywords(self) -> list[str]:
        from jobsearcher.ranking.config import unique_casefold

        return unique_casefold([k for t in self.targets for k in t.keywords])

    def by_country(self) -> dict[str, list[str]]:
        """{country: every keyword wanted anywhere in it} (for sources searched by country)."""
        from jobsearcher.ranking.config import unique_casefold

        found: dict[str, list[str]] = {}
        for t in self.targets:
            found[t.area.country] = unique_casefold(found.get(t.area.country, []) + t.keywords)
        return found


def main_keywords(config: Config) -> list[str]:
    """The profile's own search words and its role names (no aliases). A name like
    "Process / Continuous Improvement Manager" gives its parts of two words or more."""
    from jobsearcher.ranking.config import load_ranking_config, unique_casefold

    words = list(config.search.keywords)
    if config.search.use_target_roles:
        for role in load_ranking_config(config.ranking_config).target_roles:
            parts = [p.strip() for p in role.name.split("/")]
            words += [role.name] if len(parts) == 1 else [p for p in parts if len(p.split()) > 1]
    return unique_casefold(words)


def profile_areas(locations: list[str]) -> tuple[list[SearchArea], list[str]]:
    """(the areas to search, first country first; entries that aren't known places)."""
    places: list[Place] = []
    unknown: list[str] = []
    for entry in locations:
        place = resolve(entry)
        if place:
            places.append(place)
        elif entry.strip().casefold() not in {"remote", "distans", "på distans", "hybrid"}:
            unknown.append(entry)
    if not places:
        return [SearchArea(country=HOME)], unknown  # no region (or none known): all of Sweden
    countries = list(dict.fromkeys(p.country for p in places))
    areas: list[SearchArea] = []
    for country in countries:
        here = [p for p in places if p.country == country]
        names = list(dict.fromkeys(p.name for p in here))
        if any(p.kind == "country" for p in here) or len(names) > MAX_CITIES:
            areas.append(SearchArea(country=country))
        else:
            areas += [SearchArea(country=country, place=name) for name in names]
    return areas, unknown


def search_plan(profiles: list[Config]) -> SearchPlan:
    """Every profile's areas with its keywords, merged (a profile with nothing to
    search for adds nothing)."""
    from jobsearcher.pipeline import search_keywords

    merged: dict[SearchArea, Target] = {}
    unknown: list[str] = []
    for profile in profiles:
        keywords = search_keywords(profile)
        if not keywords:
            continue
        areas, missing = profile_areas(profile.search.locations)
        unknown += [m for m in missing if m not in unknown]
        main = main_keywords(profile)
        for area in areas:
            primary = area.country == areas[0].country
            words = keywords if primary else main
            target = merged.setdefault(area, Target(area=area, keywords=[], primary=primary))
            target.keywords += [w for w in words if not target.wants(w)]
            target.primary = target.primary or primary
    return SearchPlan(targets=list(merged.values()), unknown=unknown)
