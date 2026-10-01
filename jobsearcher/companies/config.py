"""The target-company list, loaded from companies.yaml."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class AtsRef(BaseModel):
    """Where a company's jobs live in an applicant-tracking system.

    `ref` is what that ATS needs to list jobs: a careers-site URL for Teamtailor,
    a customer subdomain for Varbi, a board/company identifier for the others.
    """

    type: str
    ref: str


class Company(BaseModel):
    name: str
    website: str | None = None
    org_nr: str | None = None
    # Careers page, when it isn't linked from the home page.
    careers_url: str | None = None
    # Where its jobs are when the ATS doesn't say (e.g. Varbi feeds have no location).
    location: str | None = None
    tags: list[str] = Field(default_factory=list)
    # Where this entry came from (a ranked list, a curated group, the user).
    source: str | None = None
    # Google News query; defaults to the quoted name.
    news_query: str | None = None
    # Manual override when ATS detection gets it wrong (or can't see it).
    ats: AtsRef | None = None
    enabled: bool = True

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def query(self) -> str:
        return self.news_query or f'"{self.name}"'


class CompaniesConfig(BaseModel):
    companies: list[Company] = Field(default_factory=list)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_companies(path: str | Path) -> list[Company]:
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    companies = CompaniesConfig.model_validate(raw or {}).companies
    seen: set[str] = set()
    for company in companies:
        if company.slug in seen:
            raise ValueError(f"Duplicate company in {path}: {company.name}")
        seen.add(company.slug)
    return [c for c in companies if c.enabled]
