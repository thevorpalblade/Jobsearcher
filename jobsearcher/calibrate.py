"""Calibration: the candidate rates a spread of ranked jobs; the tool shows how well the
ranking agrees, where it disagrees, better score weights, and preference changes
(web UI tab, after the preferences interview).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from pydantic import BaseModel

from jobsearcher.llm import BudgetedLLM
from jobsearcher.ranking.config import RankingConfig, Weights
from jobsearcher.ranking.ranker import final_score
from jobsearcher.store import Store
from jobsearcher.web.views import JobRow

SET_SIZE = 20  # jobs per calibration round
MIN_RATED = 12  # ratings before results are shown
# (key, score, label): how the candidate rates a job.
RATINGS = (
    ("great", 90, "Great fit"),
    ("good", 70, "Good"),
    ("maybe", 50, "Maybe"),
    ("poor", 30, "Poor"),
    ("no", 10, "No"),
)
SCORES = {key: score for key, score, _ in RATINGS}


@dataclass
class Readiness:
    ready: bool
    reason: str  # why not, in plain words ("" when ready)
    current: int  # jobs ranked with the current CVs and settings


def current_rows(rows: list[JobRow]) -> list[JobRow]:
    """Jobs ranked with today's CVs, preferences and model (not stale)."""
    return [r for r in rows if r.ranking is not None and not r.stale and r.score is not None]


def readiness(interview_done: bool, rows: list[JobRow]) -> Readiness:
    current = len(current_rows(rows))
    if not interview_done:
        return Readiness(
            False, "Do the preferences interview first, and save its settings.", current
        )
    if current < SET_SIZE:
        return Readiness(
            False,
            f"Waiting for ranking with your new settings: {current} of the {SET_SIZE} jobs "
            "needed are ranked. Ranking runs daily at 06:00 (and after a restart).",
            current,
        )
    return Readiness(True, "", current)


def pick(rows: list[JobRow], size: int = SET_SIZE) -> list[str]:
    """Job ids spread evenly over the score range, best first, so the ratings say
    something about the whole list, not only its top."""
    ranked = sorted(current_rows(rows), key=lambda r: r.score or 0, reverse=True)
    if len(ranked) <= size:
        return [r.job.id for r in ranked]
    step = (len(ranked) - 1) / (size - 1)
    return list(dict.fromkeys(ranked[round(i * step)].job.id for i in range(size)))


def agreement(pairs: list[tuple[float, float]]) -> float | None:
    """Spearman rank correlation of (candidate, model) scores; None if it can't tell."""
    if len(pairs) < 3:
        return None
    mine, model = zip(*pairs, strict=True)
    if len(set(mine)) < 2 or len(set(model)) < 2:
        return None
    return statistics.correlation(mine, model, method="ranked")


@dataclass
class Disagreement:
    row: JobRow
    mine: int
    model: int
    note: str


@dataclass
class Results:
    rated: int
    agreement: float | None
    disagreements: list[Disagreement]
    weights: Weights | None  # better weights, if they agree clearly better
    weights_agreement: float | None


def results(rows: dict[str, JobRow], ratings: list, config: RankingConfig) -> Results:
    rated = [
        (rows[r["job_id"]], SCORES[r["rating"]], r["note"] or "")
        for r in ratings
        if r["rating"] in SCORES and r["job_id"] in rows and rows[r["job_id"]].ranking
    ]
    pairs = [(float(mine), float(row.score or 0)) for row, mine, _ in rated]
    current = agreement(pairs)
    disagreements = sorted(
        (Disagreement(row, mine, row.score or 0, note) for row, mine, note in rated),
        key=lambda d: abs(d.mine - d.model),
        reverse=True,
    )
    disagreements = [d for d in disagreements if abs(d.mine - d.model) >= 25][:6]
    best, best_agreement = None, current
    for fit in range(11):
        weights = Weights(fit=fit / 10, success=1 - fit / 10)
        trial = config.model_copy(update={"weights": weights})
        score = agreement(
            [
                (float(mine), float(final_score(row.ranking.assessment, trial)))  # type: ignore[union-attr]
                for row, mine, _ in rated
            ]
        )
        if score is not None and (best_agreement is None or score > best_agreement + 0.05):
            best, best_agreement = weights, score
    if best is not None and (best.fit, best.success) == (
        config.weights.fit,
        config.weights.success,
    ):
        best = None
    return Results(len(rated), current, disagreements, best, best_agreement if best else None)


# --- preference changes from the disagreements ----------------------------------------


class PreferenceChange(BaseModel):
    situation: str
    seniority: str
    likes: list[str]
    dislikes: list[str]
    dealbreakers: list[str]
    explanation: str


SYSTEM = """\
You tune a job seeker's ranking preferences. A model scored jobs against their CV and \
these preferences; the candidate then rated some jobs themselves. Where the model and \
the candidate disagree, find what the preferences are missing or getting wrong, and \
return the full updated preferences (keep what still holds; change only what the \
disagreements and the candidate's notes support; short phrases in English). Never \
invent facts about the candidate. In explanation, say in two or three sentences what \
you changed and why.
"""


def suggest_preferences(
    llm: BudgetedLLM, config: RankingConfig, found: Results
) -> PreferenceChange:
    p = config.preferences
    current = (
        f"Situation: {p.situation}\nSeniority: {p.seniority}\nLikes: {p.likes}\n"
        f"Dislikes: {p.dislikes}\nDealbreakers: {p.dealbreakers}"
    )
    cases = []
    for d in found.disagreements:
        a = d.row.ranking.assessment  # type: ignore[union-attr]
        cases.append(
            f"- {d.row.job.title} at {d.row.job.company or '?'}: model {d.model}, candidate "
            f"{d.mine}"
            + (f' (their note: "{d.note}")' if d.note else "")
            + f"\n  Model's reasoning: {a.rationale}\n  Ad (start): "
            + " ".join(d.row.job.description.split())[:600]
        )
    prompt = f"Current preferences:\n{current}\n\nDisagreements:\n" + "\n".join(cases)
    result = llm.complete(system=SYSTEM, prompt=prompt, schema=PreferenceChange)
    if not isinstance(result.parsed, PreferenceChange):
        raise ValueError("The model's suggestion didn't fit the format; try again.")
    return result.parsed


def start_round(store: Store, job_ids: list[str]) -> None:
    store.start_calibration(job_ids)
