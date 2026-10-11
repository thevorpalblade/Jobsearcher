"""Germany's Bundesagentur für Arbeit job board (Jobbörse), its public JSON API.

Germany's largest job database, like Platsbanken for Sweden. The API is the one its
own website uses: no account, a fixed public key (`X-API-Key: jobboerse-jobsuche`);
not documented by the agency, but widely used (github.com/bundesAPI/jobsuche-api).

Searched in the profiles' German places (regions.py): by city with a radius, or the
whole country. Its search matches broadly ("Operations Manager" finds restaurant
managers), so only ads naming the keyword in the title or the agency's occupation for
the ad are opened for their full text (one request each). Every run reads all open
matching ads, so a job that stops appearing has gone (expired by absence).
"""

from __future__ import annotations

import base64
import logging
import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx

from jobsearcher.companies.http import PoliteClient
from jobsearcher.config import ArbeitsagenturConfig
from jobsearcher.contacts import extract_contacts_from_text
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.regions import Target
from jobsearcher.sources.base import parse_datetime

log = logging.getLogger(__name__)

NAME = "arbeitsagentur"
BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
HEADERS = {"X-API-Key": "jobboerse-jobsuche"}  # the public key of its own website
AD_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}"
PAGE_SIZE = 100


def _salary(hit: dict[str, Any]) -> str | None:
    low, high = hit.get("gehaltsspanneVon"), hit.get("gehaltsspanneBis")
    if low or high:
        return " – ".join(f"{float(x):.0f}" for x in (low, high) if x) + " EUR"
    return None


def parse_job(hit: dict[str, Any], details: dict[str, Any]) -> Job | None:
    """A Job from a search hit and its details (the full text)."""
    ref, title = hit.get("referenznummer"), hit.get("stellenangebotsTitel")
    if not ref or not title:
        return None
    places = hit.get("stellenlokationen") or [{}]
    address = places[0].get("adresse") or {}
    region = str(address.get("region") or "").replace("_", " ").title()
    description = str(details.get("stellenangebotsBeschreibung") or "")
    url = AD_URL.format(ref=ref)
    if hit.get("arbeitszeitVollzeit"):
        employment = "full time"
    elif any(v for k, v in hit.items() if k.startswith("arbeitszeitTeilzeit")):
        employment = "part time"
    else:
        employment = None
    return Job(
        id=make_job_id(NAME, ref),
        title=" ".join(str(title).split()),
        company=hit.get("firma"),
        location=address.get("ort"),  # "Haar, Kreis München"
        region=", ".join(x for x in (region, "Germany") if x),
        remote=hit.get("homeofficeprozent") == 100 or None,
        description=description,
        employment_type=employment,
        salary=_salary(hit),
        url=url,
        apply_url=hit.get("externeURL") or url,  # ads from other boards apply there
        published_at=parse_datetime(hit.get("datumErsteVeroeffentlichung")),
        contacts=extract_contacts_from_text(description, provenance=f"{NAME}:ad_text"),
        sources=[SourceRef(source=NAME, source_id=ref, url=url)],
    )


class ArbeitsagenturSource:
    name = NAME

    def __init__(self, client: PoliteClient, config: ArbeitsagenturConfig, targets: list[Target]):
        self.client = client
        self.config = config
        self.targets = [t for t in targets if t.area.country == "DE"]
        self._details: dict[str, dict[str, Any] | None] = {}  # read this run, by reference

    def search(self, keyword: str, published_after: datetime | None = None) -> Iterator[Job]:
        """The open ads in the German areas wanting `keyword` that name it (whole word
        or phrase) in their title or occupation."""
        pattern = re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.I)
        for target in self.targets:
            if not target.wants(keyword):
                continue
            params: dict[str, Any] = {"was": keyword, "angebotsart": 1, "size": PAGE_SIZE}
            if target.area.place:
                params |= {"wo": target.area.place, "umkreis": self.config.radius_km}
            for hit in self._hits(params):
                occupations = [hit.get("hauptberuf"), *(hit.get("alleBerufe") or [])]
                named = " ".join(
                    str(x) for x in [hit.get("stellenangebotsTitel"), *occupations] if x
                )
                if not pattern.search(named):
                    continue
                details = self._details_of(hit["referenznummer"])
                if details is not None and (job := parse_job(hit, details)):
                    job.near = target.area.place  # within radius_km of it
                    yield job

    def _hits(self, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            data = self.client.get_json(f"{BASE}/pc/v6/jobs", {**params, "page": page}, HEADERS)
            hits = data.get("ergebnisliste") or []
            yield from (h for h in hits if h.get("referenznummer"))
            total = int(data.get("maxErgebnisse") or 0)
            if not hits or page * PAGE_SIZE >= min(total, self.config.max_results):
                return
            page += 1

    def _details_of(self, ref: str) -> dict[str, Any] | None:
        if ref not in self._details:
            encoded = base64.b64encode(ref.encode()).decode()
            try:
                self._details[ref] = self.client.get_json(
                    f"{BASE}/pc/v4/jobdetails/{encoded}", headers=HEADERS
                )
            except httpx.HTTPError as exc:  # one bad ad doesn't stop the rest
                log.info("%s: details of %s failed: %s", NAME, ref, exc)
                self._details[ref] = None
        return self._details[ref]
