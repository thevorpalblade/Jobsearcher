"""Monthly LLM budget: cost accounting and enforcement."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic import BaseModel

from jobsearcher.config import DEFAULT_PRICES, LLMConfig, ModelPrice
from jobsearcher.llm.base import BudgetExceeded, LLMClient, LLMError, LLMResult, LLMUsage
from jobsearcher.store import Store

log = logging.getLogger(__name__)

# Used for models with no known price, so unknown models are over- rather than under-counted.
_FALLBACK_PRICE = ModelPrice(
    input=max(p.input for p in DEFAULT_PRICES.values()),
    output=max(p.output for p in DEFAULT_PRICES.values()),
    cache_read=max(p.cache_read for p in DEFAULT_PRICES.values()),
    cache_write=max(p.cache_write or p.input for p in DEFAULT_PRICES.values()),
)


def _month_start(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class BudgetTracker:
    def __init__(self, store: Store, config: LLMConfig):
        self.store = store
        self.config = config

    def cost(self, usage: LLMUsage) -> float:
        if not usage.billed:
            return 0.0
        price = self.config.price_for(usage.model)
        if price is None:
            log.warning("No price configured for %s; assuming the highest known price", usage.model)
            price = _FALLBACK_PRICE
        cache_write = price.cache_write if price.cache_write is not None else price.input
        return (
            usage.input_tokens * price.input
            + usage.output_tokens * price.output
            + usage.cache_read_tokens * price.cache_read
            + usage.cache_write_tokens * cache_write
        ) / 1_000_000

    def month_to_date(self, now: datetime | None = None) -> float:
        return self.store.llm_cost_since(_month_start(now))

    def subscription_usage(self, now: datetime | None = None) -> tuple[int, int]:
        """(calls, tokens) this month that went through Claude Code on the subscription."""
        return self.store.llm_calls_since(_month_start(now), "claude-code/")

    def limit_for(self, purpose: str) -> float:
        budget = self.config.monthly_budget_usd
        return budget * self.config.drafting_budget_share if purpose == "drafting" else budget

    def check(self, purpose: str) -> None:
        spent, limit = self.month_to_date(), self.limit_for(purpose)
        if spent >= limit:
            raise BudgetExceeded(
                f"{purpose} paused: ${spent:.2f} spent this month (limit ${limit:.2f})"
            )

    def record(self, usage: LLMUsage, purpose: str) -> float:
        cost = self.cost(usage)
        self.store.record_llm_usage(
            datetime.now(UTC),
            usage.model,
            purpose,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            cost,
        )
        return cost


class BudgetedLLM:
    """Wraps an LLMClient: refuses calls over budget and records the cost of every call."""

    def __init__(self, client: LLMClient, tracker: BudgetTracker, purpose: str):
        self.client = client
        self.tracker = tracker
        self.purpose = purpose

    @property
    def model(self) -> str:
        return self.client.model

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        context: str = "",
        schema: type[BaseModel] | None = None,
    ) -> LLMResult:
        if getattr(self.client, "metered", True):
            self.tracker.check(self.purpose)
        try:
            result = self.client.complete(
                system=system, prompt=prompt, context=context, schema=schema
            )
        except LLMError as exc:
            if exc.usage is not None:
                self.tracker.record(exc.usage, self.purpose)
            raise
        self.tracker.record(result.usage, self.purpose)
        return result
