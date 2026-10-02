"""Ranking configuration, loaded from ranking.yaml."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


def unique_casefold(items: list[str]) -> list[str]:
    """Drop blanks and case-insensitive duplicates, keeping the first spelling."""
    seen: dict[str, str] = {}
    for item in items:
        item = item.strip()
        if item and item.casefold() not in seen:
            seen[item.casefold()] = item
    return list(seen.values())


class TargetRole(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    # Occupation filters, matched case-insensitively as substrings of the job's
    # occupation field or group (`jobsearcher occupations` lists them). A job only
    # counts as this role if it matches none of `exclude_occupations` (unless it also
    # matches `except_occupations`, e.g. to keep one group of an excluded field) and,
    # when `include_occupations` is set, at least one of those. Jobs without
    # occupation data always pass.
    exclude_occupations: list[str] = Field(default_factory=list)
    except_occupations: list[str] = Field(default_factory=list)
    include_occupations: list[str] = Field(default_factory=list)

    @property
    def terms(self) -> list[str]:
        return unique_casefold([self.name, *self.aliases])

    def occupation_verdict(self, field: str | None, group: str | None) -> tuple[bool, str | None]:
        """Whether the occupation filters let a job with this field/group count as this
        role, and if not, why (shown in the web UI)."""
        labels = [x.casefold() for x in (field, group) if x]
        if not labels:
            return True, None

        def first_hit(patterns: list[str]) -> str | None:
            return next(
                (p for p in patterns if any(p.casefold() in label for label in labels)), None
            )

        excluded = first_hit(self.exclude_occupations)
        if excluded is not None and first_hit(self.except_occupations) is None:
            return False, f"occupation excluded ({excluded})"
        if self.include_occupations and first_hit(self.include_occupations) is None:
            return False, "occupation not in include_occupations"
        return True, None

    def allows_occupation(self, field: str | None, group: str | None) -> bool:
        return self.occupation_verdict(field, group)[0]


class Preferences(BaseModel):
    # Facts about the candidate that the CV doesn't show (where they live, work permit
    # status, ...), and how the model should weigh them.
    situation: str = ""
    seniority: str = ""
    languages: str = ""
    likes: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    dealbreakers: list[str] = Field(default_factory=list)


class Weights(BaseModel):
    fit: float = 0.6
    success: float = 0.4

    def combine(self, fit: int, success: int) -> int:
        total = self.fit + self.success
        if total <= 0:
            return round((fit + success) / 2)
        return round((fit * self.fit + success * self.success) / total)


class Adjustments(BaseModel):
    """Points added to the combined score (0-100) for facts the model extracts from the ad.
    Applied in code, so changing them doesn't trigger re-ranking."""

    english_ad: int = 10  # the ad is written in English
    swedish_required: int = -20  # Swedish is an explicit requirement (not just a merit)
    swedish_merit: int = -5  # Swedish is mentioned as a merit / nice to have


class Prefilter(BaseModel):
    require_role_match: bool = True
    max_llm_calls_per_run: int = 100


class DraftingThresholds(BaseModel):
    min_score: int = 70
    max_drafts_per_day: int = 5


class RankingConfig(BaseModel):
    target_roles: list[TargetRole] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)
    weights: Weights = Field(default_factory=Weights)
    adjustments: Adjustments = Field(default_factory=Adjustments)
    prefilter: Prefilter = Field(default_factory=Prefilter)
    drafting: DraftingThresholds = Field(default_factory=DraftingThresholds)

    @property
    def role_terms(self) -> list[str]:
        return unique_casefold([t for role in self.target_roles for t in role.terms])

    def fingerprint(self) -> str:
        """Hash of the parts that affect LLM scores (not limits, thresholds or filters)."""
        relevant = self.model_dump(
            include={"target_roles": {"__all__": {"name", "aliases"}}, "preferences": True}
        )
        return hashlib.sha256(repr(relevant).encode()).hexdigest()[:16]

    def filter_fingerprint(self) -> str:
        """Hash of everything the prefilter depends on: unlike fingerprint(), this
        includes the occupation filters (used to memoise prefilter results)."""
        relevant = self.model_dump(include={"target_roles": True, "prefilter": True})
        return hashlib.sha256(repr(relevant).encode()).hexdigest()[:16]


def load_ranking_config(path: str | Path) -> RankingConfig:
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    return RankingConfig.model_validate(raw or {})
