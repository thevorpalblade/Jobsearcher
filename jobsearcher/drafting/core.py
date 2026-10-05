"""Draft a tailored CV and cover letter, check them against the CVs, repair once.

The generation model (Claude Code on the subscription) writes; a different model
(GLM, free) verifies every claim about the candidate against the CVs. Anything still
unsupported after one repair round marks the draft "needs review". Numbers get an
extra, model-free check: a figure that appears in no source text is flagged.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from jobsearcher.drafting import prompts
from jobsearcher.llm import BudgetedLLM, LLMError
from jobsearcher.store import Store

log = logging.getLogger(__name__)


class Claim(BaseModel):
    claim: str = Field(description="One factual statement about the candidate")
    supported: bool = Field(description="True if the CVs support it")
    evidence: str | None = Field(
        description="A short quote from the CVs that supports it; null when unsupported"
    )


class DraftContent(BaseModel):
    """What the drafting model returns."""

    cover_letter: str = Field(description="The cover letter, in Markdown")
    cv: str = Field(description="The tailored CV, in Markdown")
    notes: list[str] = Field(description="Plain-English notes for the candidate")


class GroundingCheck(BaseModel):
    claims: list[Claim]


class Draft(BaseModel):
    """A stored draft."""

    key: str  # a job id, or "company:<slug>" for a spontaneous application
    input_hash: str
    created_at: datetime
    trigger: Literal["manual", "shortlist"] = "manual"
    model: str
    check_model: str | None = None
    base_cv: str
    instructions: str = ""
    addressed_to: str | None = None
    cover_letter: str
    cv: str
    notes: list[str] = Field(default_factory=list)
    claims_checked: int = 0
    flagged: list[Claim] = Field(default_factory=list)
    repaired: bool = False
    check_error: str | None = None
    files: list[str] = Field(default_factory=list)  # file names in the version folder

    @property
    def needs_review(self) -> bool:
        return bool(self.flagged) or self.check_error is not None


@dataclass
class Request:
    """What to draft."""

    key: str
    prompt: str  # the task: ad text and requirements, or the spontaneous brief
    identity: list[str] = field(default_factory=list)  # parts of the target that change the draft
    instructions: str = ""
    addressed_to: str | None = None
    sources: list[str] = field(
        default_factory=list
    )  # texts besides the CVs that figures may come from


Renderer = Callable[[Path, str, str], list[str]]


def draft_hash(request: Request, cvs: list[tuple[str, str]], model: str) -> str:
    parts = [request.key, *request.identity, request.instructions.strip(), model]
    parts += [prompts.PROMPT_VERSION] + [f"{name}\x1e{text}" for name, text in cvs]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


_NUMBER = re.compile(r"(?<![\w.])\d[\d.,]*(?:\+|%)?")
# Years and one- or two-digit figures that are plain text structure, not claims.
_IGNORED = {"1", "2", "3", "4", "5", "6", "7", "8", "9"}


def numbers_in(text: str) -> set[str]:
    return {m.group().rstrip(".,") for m in _NUMBER.finditer(text)} - _IGNORED


def unsupported_numbers(text: str, sources: list[str]) -> list[Claim]:
    """Figures in `text` that appear in none of the `sources` (a model-free safety net:
    an invented "30% faster" or "12 years" is caught even if the model check misses it)."""
    known = set()
    for source in sources:
        known |= numbers_in(source)
    known_plain = {n.rstrip("+%") for n in known}
    out = []
    for number in sorted(numbers_in(text)):
        if number in known or number.rstrip("+%") in known_plain:
            continue
        out.append(
            Claim(
                claim=f"The figure “{number}” appears in the draft",
                supported=False,
                evidence=None,
            )
        )
    return out


def _check(
    checkers: list[BudgetedLLM], context: str, draft: DraftContent
) -> tuple[list[Claim], BudgetedLLM | None, str | None]:
    """The first checker that answers: (claims, the checker, error). The checkers are
    tried in order, so a slow or failing primary falls back to the next one."""
    error = None
    for checker in checkers:
        try:
            result = checker.complete(
                system=prompts.CHECK_SYSTEM,
                context=context,
                prompt=prompts.check_prompt(draft.cv, draft.cover_letter),
                schema=GroundingCheck,
            )
            return result.parsed.claims, checker, None  # type: ignore[union-attr]
        except LLMError as exc:
            error = f"The grounding check couldn't run: {exc}"
            log.warning("Grounding check by %s failed: %s", checker.model, exc)
    return [], None, error


def check_draft(
    checkers: list[BudgetedLLM],
    context: str,
    draft: DraftContent,
    cv_texts: list[str],
    sources: list[str],
) -> tuple[list[Claim], int, BudgetedLLM | None, str | None]:
    """(unsupported claims, claims checked, the checker that answered, error)."""
    claims, answered, error = _check(checkers, context, draft)
    flagged = [c for c in claims if not c.supported]
    flagged += unsupported_numbers(draft.cv, cv_texts)
    flagged += unsupported_numbers(draft.cover_letter, cv_texts + sources)
    return flagged, len(claims), answered, error


def generate_draft(
    store: Store,
    request: Request,
    cvs: list[tuple[str, str]],
    draft_llm: BudgetedLLM,
    check_llm: BudgetedLLM,
    out_dir: Path,
    render: Renderer,
    trigger: Literal["manual", "shortlist"] = "manual",
    fallback_llm: BudgetedLLM | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> Draft:
    """The draft for `request`: the cached one when nothing changed, else a new one."""
    input_hash = draft_hash(request, cvs, draft_llm.model)
    cached = store.get_draft(request.key, input_hash)
    if cached is not None and not force:
        return Draft.model_validate_json(cached["data"])

    context = prompts.cv_context(cvs)
    cv_texts = [text for _, text in cvs]
    content: DraftContent = draft_llm.complete(
        system=prompts.DRAFT_SYSTEM, context=context, prompt=request.prompt, schema=DraftContent
    ).parsed  # type: ignore[assignment]
    checkers = [check_llm] + ([fallback_llm] if fallback_llm else [check_llm])  # no fallback: retry
    flagged, checked, answered, error = check_draft(
        checkers, context, content, cv_texts, request.sources
    )
    if answered is not None:  # the re-check starts with whoever answered, not a slow primary
        checkers = [answered] + [c for c in checkers if c is not answered]

    repaired = False
    if flagged and error is None:
        # One repair round: show the model what wasn't supported and have it rewrite.
        repaired = True
        again = draft_llm.complete(
            system=prompts.DRAFT_SYSTEM,
            context=context,
            prompt=prompts.repair_prompt(
                request.prompt, content.cv, content.cover_letter, [c.claim for c in flagged]
            ),
            schema=DraftContent,
        ).parsed
        content = again  # type: ignore[assignment]
        flagged, checked, answered, error = check_draft(
            checkers, context, content, cv_texts, request.sources
        )

    version_dir = out_dir / _safe(request.key) / input_hash
    files = render(version_dir, content.cv, content.cover_letter)
    draft = Draft(
        key=request.key,
        input_hash=input_hash,
        created_at=now or datetime.now(UTC),
        trigger=trigger,
        model=draft_llm.model,
        check_model=answered.model if answered else None,
        base_cv=cvs[0][0],
        instructions=request.instructions.strip(),
        addressed_to=request.addressed_to,
        cover_letter=content.cover_letter.strip(),
        cv=content.cv.strip(),
        notes=[n.strip() for n in content.notes if n.strip()],
        claims_checked=checked,
        flagged=flagged,
        repaired=repaired,
        check_error=error,
        files=files,
    )
    store.save_draft(request.key, input_hash, draft.model_dump_json())
    return draft


def _safe(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", key).strip("-") or "draft"
