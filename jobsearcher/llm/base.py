"""Provider-neutral LLM interface used by the ranking and drafting stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


class LLMError(RuntimeError):
    def __init__(self, message: str, usage: LLMUsage | None = None):
        super().__init__(message)
        # Set when the request was billed even though it failed (refusal, max_tokens).
        self.usage = usage


class LLMRefusal(LLMError):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass
class LLMUsage:
    model: str  # the model that actually served the request
    input_tokens: int  # uncached input
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # False when covered by a subscription (Claude Code) rather than paid per token.
    billed: bool = True


@dataclass
class LLMResult:
    text: str
    usage: LLMUsage
    parsed: BaseModel | None = None


class LLMClient(Protocol):
    model: str

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        context: str = "",
        schema: type[BaseModel] | None = None,
    ) -> LLMResult:
        """Run one request.

        `context` is large text that stays identical across calls (e.g. the master CV).
        It is placed right after the system prompt so the provider's prompt cache can
        reuse it. When `schema` is given the reply is validated into `result.parsed`.
        """
        ...
