"""Cheap, LLM-free filtering and ordering before jobs are sent for scoring."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache

from jobsearcher.models import Job
from jobsearcher.ranking.config import RankingConfig


@lru_cache(maxsize=256)
def _terms_pattern(terms: tuple[str, ...]) -> re.Pattern[str]:
    # One alternation per role instead of one search per term: same matches (the
    # regex engine backtracks into the next alternative when a boundary fails), ~4x faster.
    alternatives = "|".join(re.escape(t.casefold()) for t in terms)
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")


def _contains_any(text: str, terms: list[str]) -> bool:
    # Whole-word match so "HR" in a term doesn't match inside other words.
    return bool(terms) and _terms_pattern(tuple(terms)).search(text) is not None


@dataclass
class PrefilterResult:
    passed: bool
    # Target roles the job counts as (mentioned, and allowed by the occupation filters).
    roles: list[str] = field(default_factory=list)
    # Whether any of `roles` is mentioned in the title.
    in_title: bool = False
    # Every role mentioned, ignoring occupation filters, and those only in the ad text.
    roles_unfiltered: list[str] = field(default_factory=list)
    body_only: list[str] = field(default_factory=list)
    # Mentioned roles that the occupation filters rejected, with the reason.
    excluded: dict[str, str] = field(default_factory=dict)


def prefilter_status(job: Job, config: RankingConfig) -> PrefilterResult:
    """Which target roles a job mentions, and whether it passes the prefilter."""
    title = job.title.casefold()
    body = job.description.casefold()
    result = PrefilterResult(passed=False)
    for role in config.target_roles:
        terms = role.terms
        hit_title = _contains_any(title, terms)
        if not hit_title and not _contains_any(body, terms):
            continue
        result.roles_unfiltered.append(role.name)
        if not hit_title:
            result.body_only.append(role.name)
        allowed, reason = role.occupation_verdict(job.occupation_field, job.occupation_group)
        if not allowed:
            result.excluded[role.name] = reason or "occupation filter"
            continue
        result.roles.append(role.name)
        result.in_title = result.in_title or hit_title
    result.passed = bool(
        result.roles or not (config.prefilter.require_role_match and config.target_roles)
    )
    return result


def matched_roles(
    job: Job, config: RankingConfig, apply_occupation_filters: bool = True
) -> tuple[list[str], bool]:
    """Names of target roles the job mentions, and whether any matched in the title.
    A role whose occupation filters reject the job doesn't count."""
    status = prefilter_status(job, config)
    if apply_occupation_filters:
        return status.roles, status.in_title
    in_title = any(name not in status.body_only for name in status.roles_unfiltered)
    return status.roles_unfiltered, in_title


def select_for_ranking(jobs: list[Job], config: RankingConfig) -> list[Job]:
    """Drop jobs that mention no target role (if required); title matches and newer ads first."""
    candidates: list[tuple[bool, datetime, Job]] = []
    for job in jobs:
        status = prefilter_status(job, config)
        if not status.passed:
            continue
        published = job.published_at or datetime.min.replace(tzinfo=UTC)
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        candidates.append((status.in_title, published, job))
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    return [job for _, _, job in candidates]
