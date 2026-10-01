"""Classify company news into signals for spontaneous applications, with the LLM."""

from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from jobsearcher.companies.config import Company
from jobsearcher.llm import BudgetedLLM, BudgetExceeded, LLMError, LLMResult
from jobsearcher.store import Store

log = logging.getLogger(__name__)

# Bump when the prompt or schema changes; older signals are then re-classified.
PROMPT_VERSION = "1"
# Headlines per LLM call; a busy company's news is split over several calls.
BATCH_SIZE = 25

Kind = Literal[
    "merger_acquisition",
    "expansion",
    "funding",
    "leadership_change",
    "reorganisation",
    "layoffs",
    "other",
    "not_about_company",
]


class NewsSignal(BaseModel):
    index: int = Field(description="The headline's number in the list")
    kind: Kind
    relevance: int = Field(
        ge=0,
        le=100,
        description="How strongly this suggests the company may soon need someone with "
        "the candidate's profile (0 = not at all, 100 = very likely)",
    )
    summary: str = Field(description="One sentence: what happened and why it matters for her")


class NewsAssessment(BaseModel):
    signals: list[NewsSignal]


SYSTEM_PROMPT = """\
You help a job seeker find companies worth a spontaneous application (spontanansökan): \
companies where something is happening that may soon create a need for someone with her \
profile, before any job is advertised. Her master CV and target roles follow.

You get recent news headlines about one company (title, date and source domain only). \
For each headline:
- kind: merger_acquisition, expansion (new sites, markets, products, large contracts), \
funding, leadership_change, reorganisation, layoffs, other, or not_about_company when the \
headline is about something else that shares the name.
- relevance 0-100: how strongly this suggests the company will soon need someone like her. \
Mergers and acquisitions (integration work), expansion, reorganisations and new leadership \
in operations, HR, change management or healthcare administration score high. Routine \
news, results, products, opinion pieces and anything not about the company score low. \
Layoffs are usually low, unless they come with a reorganisation she could help run.
- summary: one sentence on what happened and why it matters (or doesn't) for her.
Judge only from the headline; don't invent details. Return one entry per headline.
"""


def company_prompt(company: Company, rows: list) -> str:
    lines = [f"Company: {company.name}", "", "Headlines:"]
    for i, row in enumerate(rows, 1):
        date = (row["published_at"] or "")[:10]
        lines.append(f"{i}. [{date}, {row['domain'] or '?'}] {row['title']}")
    return "\n".join(lines)


@dataclass
class ClassifyReport:
    items: int = 0
    classified: int = 0
    failed_batches: int = 0
    stopped_reason: str | None = None


def classify_news(
    store: Store,
    llm: BudgetedLLM,
    companies: list[Company],
    context: str,
    max_parallel: int = 1,
) -> ClassifyReport:
    """Classify every unclassified news item, one LLM call per batch of a company's
    headlines. Requests run in worker threads; store access stays on this thread."""
    by_slug = {c.slug: c for c in companies}
    pending_rows: dict[str, list] = defaultdict(list)
    for row in store.unclassified_news(PROMPT_VERSION):
        if row["company"] in by_slug:
            pending_rows[row["company"]].append(row)
    batches = [
        (by_slug[slug], rows[i : i + BATCH_SIZE])
        for slug, rows in pending_rows.items()
        for i in range(0, len(rows), BATCH_SIZE)
    ]
    report = ClassifyReport(items=sum(len(rows) for _, rows in batches))

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures: dict[Future[LLMResult], tuple[Company, list]] = {}
        queue = list(batches)
        while queue or futures:
            while queue and len(futures) < max_parallel:
                try:
                    llm.check()
                except BudgetExceeded as exc:
                    report.stopped_reason = str(exc)
                    queue.clear()
                    break
                company, rows = queue.pop(0)
                future = pool.submit(
                    llm.call,
                    system=SYSTEM_PROMPT,
                    context=context,
                    prompt=company_prompt(company, rows),
                    schema=NewsAssessment,
                )
                futures[future] = (company, rows)
            if not futures:
                break
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                company, rows = futures.pop(future)
                try:
                    result = future.result()
                except LLMError as exc:
                    if exc.usage is not None:
                        llm.record(exc.usage)
                    log.warning("Classifying news for %s failed: %s", company.name, exc)
                    report.failed_batches += 1
                    continue
                llm.record(result.usage)
                assessment: NewsAssessment = result.parsed  # type: ignore[assignment]
                for signal in assessment.signals:
                    if 1 <= signal.index <= len(rows):
                        row = rows[signal.index - 1]
                        store.save_signal(
                            row["id"],
                            signal.kind,
                            signal.relevance,
                            signal.summary,
                            result.usage.model,
                            PROMPT_VERSION,
                        )
                        report.classified += 1
    return report
