"""Cheap, LLM-free filtering and ordering before jobs are sent for scoring."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from jobsearcher.models import Job
from jobsearcher.ranking.config import RankingConfig


def _contains(text: str, term: str) -> bool:
    # Whole-word match so "HR" in a term doesn't match inside other words.
    return re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", text) is not None


def matched_roles(
    job: Job, config: RankingConfig, apply_occupation_filters: bool = True
) -> tuple[list[str], bool]:
    """Names of target roles the job mentions, and whether any matched in the title.
    A role whose occupation filters reject the job doesn't count."""
    title = job.title.casefold()
    body = job.description.casefold()
    names: list[str] = []
    in_title = False
    for role in config.target_roles:
        if apply_occupation_filters and not role.allows_occupation(
            job.occupation_field, job.occupation_group
        ):
            continue
        hit_title = any(_contains(title, t) for t in role.terms)
        if hit_title or any(_contains(body, t) for t in role.terms):
            names.append(role.name)
            in_title = in_title or hit_title
    return names, in_title


def select_for_ranking(jobs: list[Job], config: RankingConfig) -> list[Job]:
    """Drop jobs that mention no target role (if required); title matches and newer ads first."""
    candidates: list[tuple[bool, datetime, Job]] = []
    for job in jobs:
        roles, in_title = matched_roles(job, config)
        if config.prefilter.require_role_match and config.target_roles and not roles:
            continue
        published = job.published_at or datetime.min.replace(tzinfo=UTC)
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        candidates.append((in_title, published, job))
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    return [job for _, _, job in candidates]
