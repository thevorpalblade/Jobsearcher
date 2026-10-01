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

    @property
    def terms(self) -> list[str]:
        return unique_casefold([self.name, *self.aliases])


class Preferences(BaseModel):
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
        """Hash of the parts that affect LLM scores (not limits or thresholds)."""
        relevant = self.model_dump(include={"target_roles", "preferences"})
        return hashlib.sha256(repr(relevant).encode()).hexdigest()[:16]


def load_ranking_config(path: str | Path) -> RankingConfig:
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    return RankingConfig.model_validate(raw or {})
