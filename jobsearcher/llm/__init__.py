"""LLM providers (Kimi, NVIDIA-hosted models, Claude) behind one interface, plus budgets."""

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
from jobsearcher.llm.ratelimit import limiter_for

Role = Literal["ranking", "drafting", "grounding", "grounding_fallback"]


def _spec(config: Config, role: Role):  # type: ignore[no-untyped-def]
    llm = config.llm
    if role == "ranking":
        return llm.ranking
    if role == "drafting":
        return llm.drafting
    if role == "grounding":
        # Unset: the ranking model, but quick to give up, so a busy provider doesn't hold
        # up a draft (the fallback, if any, then takes over).
        return llm.grounding or llm.ranking.model_copy(update={"timeout_s": 150, "max_retries": 0})
    if llm.grounding_fallback is None:
        raise LLMError("No grounding fallback model configured")
    return llm.grounding_fallback


def make_llm(config: Config, role: Role, tracker: BudgetTracker) -> BudgetedLLM:
    """Build the client configured for `role`, wrapped with budget enforcement."""
    spec = _spec(config, role)
    client: LLMClient
    if spec.provider == Provider.CLAUDE_CODE:
        from jobsearcher.llm.claude_code_client import ClaudeCodeLLM

        client = ClaudeCodeLLM(spec.model, effort=spec.effort, timeout_s=spec.timeout_s)
    elif spec.provider == Provider.ANTHROPIC:
        from jobsearcher.llm.anthropic_client import AnthropicLLM

        client = AnthropicLLM(spec.model, effort=spec.effort, max_tokens=spec.max_tokens)
    elif spec.provider == Provider.NVIDIA:
        from jobsearcher.llm.openai_compatible import OpenAICompatibleLLM

        client = OpenAICompatibleLLM(
            spec.model,
            base_url=config.llm.nvidia_base_url,
            api_key_env="NVIDIA_API_KEY",
            label="NVIDIA",
            max_tokens=spec.max_tokens,
            extra_body=spec.extra_body,
            enforce_schema=spec.enforce_schema,
            timeout_s=spec.timeout_s,
            max_retries=spec.max_retries,
            limiter=limiter_for("nvidia", config.llm.nvidia_requests_per_minute),
        )
    elif spec.provider == Provider.OLLAMA:
        from jobsearcher.llm.openai_compatible import OpenAICompatibleLLM

        client = OpenAICompatibleLLM(
            spec.model,
            base_url=config.llm.ollama_base_url,
            api_key_env=None,
            label="Ollama",
            max_tokens=spec.max_tokens,
            extra_body=spec.extra_body,
            enforce_schema=spec.enforce_schema,
            timeout_s=spec.timeout_s,
            max_retries=spec.max_retries,
        )
    else:
        from jobsearcher.llm.openai_compatible import OpenAICompatibleLLM

        client = OpenAICompatibleLLM(
            spec.model,
            base_url=config.llm.moonshot_base_url,
            max_tokens=spec.max_tokens,
            extra_body=spec.extra_body,
            enforce_schema=spec.enforce_schema,
            timeout_s=spec.timeout_s,
            max_retries=spec.max_retries,
            limiter=limiter_for("moonshot", config.llm.moonshot_requests_per_minute),
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
