"""Claude models via the official Anthropic SDK. Reads ANTHROPIC_API_KEY."""

from __future__ import annotations

from typing import Any

import anthropic
from pydantic import BaseModel

from jobsearcher.llm.base import LLMError, LLMRefusal, LLMResult, LLMUsage

# Models that accept the server-side refusal fallback (`fallbacks: "default"`): if the
# model declines, the API re-runs the request on a suitable fallback model in the same call.
_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    def __init__(
        self,
        model: str,
        effort: str | None = None,
        max_tokens: int = 16000,
        client: anthropic.Anthropic | None = None,
    ):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = client or anthropic.Anthropic()

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        context: str = "",
        schema: type[BaseModel] | None = None,
    ) -> LLMResult:
        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if context:
            # Cache breakpoint after the stable prefix (system + context). The per-job
            # prompt comes after it, so every call for the same CV reuses the cache.
            system_blocks.append(
                {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}
            )

        output_config: dict[str, Any] = {}
        if schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": anthropic.transform_schema(schema),
            }
        if self.effort:
            output_config["effort"] = self.effort

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_blocks,
            "messages": [{"role": "user", "content": prompt}],
        }
        if output_config:
            kwargs["output_config"] = output_config

        try:
            if self.model in _FALLBACK_MODELS:
                response = self.client.beta.messages.create(
                    betas=[_FALLBACK_BETA], fallbacks="default", **kwargs
                )
            else:
                response = self.client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Anthropic API unreachable: {exc}") from exc
        except (anthropic.AnthropicError, TypeError) as exc:
            # e.g. no credentials configured (no ANTHROPIC_API_KEY / `ant auth login`)
            raise LLMError(f"Anthropic client error: {exc}") from exc

        # With a refusal fallback, `response.model` is the model that finished the request;
        # the whole call is priced at its rate, which is close enough for budgeting.
        usage = LLMUsage(
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_input_tokens or 0,
            cache_write_tokens=response.usage.cache_creation_input_tokens or 0,
        )
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise LLMRefusal(f"{response.model} declined the request (category: {category})", usage)
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{response.model} hit max_tokens={self.max_tokens}", usage)

        text = "".join(block.text for block in response.content if block.type == "text")
        parsed = schema.model_validate_json(text) if schema is not None else None
        return LLMResult(text=text, usage=usage, parsed=parsed)
