"""LLM providers (Moonshot/Kimi, Anthropic/Claude) behind one interface, plus budget tracking."""

from __future__ import annotations

from typing import Literal

from jobsearcher.config import Config, Provider
from jobsearcher.llm.base import (
    BudgetExceeded,
    LLMClient,
    LLMError,
    LLMRefusal,
    LLMResult,
    LLMUsage,
)
from jobsearcher.llm.budget import BudgetedLLM, BudgetTracker

Role = Literal["ranking", "drafting"]


def make_llm(config: Config, role: Role, tracker: BudgetTracker) -> BudgetedLLM:
    """Build the client configured for `role`, wrapped with budget enforcement."""
    spec = config.llm.ranking if role == "ranking" else config.llm.drafting
    client: LLMClient
    if spec.provider == Provider.CLAUDE_CODE:
        from jobsearcher.llm.claude_code_client import ClaudeCodeLLM

        client = ClaudeCodeLLM(spec.model, effort=spec.effort, timeout_s=spec.timeout_s)
    elif spec.provider == Provider.ANTHROPIC:
        from jobsearcher.llm.anthropic_client import AnthropicLLM

        client = AnthropicLLM(spec.model, effort=spec.effort, max_tokens=spec.max_tokens)
    else:
        from jobsearcher.llm.moonshot_client import MoonshotLLM

        client = MoonshotLLM(
            spec.model, base_url=config.llm.moonshot_base_url, max_tokens=spec.max_tokens
        )
    return BudgetedLLM(client, tracker, purpose=role)


__all__ = [
    "BudgetExceeded",
    "BudgetTracker",
    "BudgetedLLM",
    "LLMClient",
    "LLMError",
    "LLMRefusal",
    "LLMResult",
    "LLMUsage",
    "make_llm",
]
