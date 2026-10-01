"""Normalised data model shared by every pipeline stage."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    OPEN = "open"
    EXPIRED = "expired"


class Contact(BaseModel):
    name: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    # Where this contact came from, e.g. "platsbanken:application_contacts" or "ad_text".
    provenance: str

    def key(self) -> tuple[str, str, str]:
        return (
            (self.name or "").strip().lower(),
            (self.email or "").strip().lower(),
            re.sub(r"\D", "", self.phone or ""),
        )


class SourceRef(BaseModel):
    """One listing of the job on one source."""

    source: str
    source_id: str
    url: str | None = None


class Job(BaseModel):
    id: str
    title: str
    company: str | None = None
    company_org_nr: str | None = None
    location: str | None = None
    region: str | None = None
    remote: bool | None = None
    description: str = ""
    language: str | None = None
    employment_type: str | None = None
    salary: str | None = None
    url: str | None = None
    apply_url: str | None = None
    apply_email: str | None = None
    published_at: datetime | None = None
    deadline: datetime | None = None
    contacts: list[Contact] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    status: JobStatus = JobStatus.OPEN

    @property
    def dedupe_key(self) -> str:
        return dedupe_key(self.company, self.title, self.location)

    @property
    def content_hash(self) -> str:
        """Changes when the parts of the ad that ranking/drafting depend on change."""
        payload = "\x1f".join([self.title, self.company or "", self.description])
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def make_job_id(source: str, source_id: str) -> str:
    return hashlib.sha256(f"{source}:{source_id}".encode()).hexdigest()[:16]


_COMPANY_SUFFIXES = re.compile(r"\b(ab|aktiebolag|publ|hb|kb|ek för|ekonomisk förening|ltd|inc)\b")


def _normalise(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe_key(company: str | None, title: str | None, location: str | None) -> str:
    company_n = _COMPANY_SUFFIXES.sub(" ", _normalise(company))
    company_n = re.sub(r"\s+", " ", company_n).strip()
    parts = [company_n, _normalise(title), _normalise(location)]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
