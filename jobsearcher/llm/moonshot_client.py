"""Kimi models via Moonshot's OpenAI-compatible API. Reads MOONSHOT_API_KEY."""

from __future__ import annotations

import json
import os
from typing import Any

import openai
from pydantic import BaseModel, ValidationError

from jobsearcher.llm.base import LLMError, LLMResult, LLMUsage


class MoonshotLLM:
    def __init__(
        self,
        model: str,
        base_url: str = "https://api.moonshot.ai/v1",
        max_tokens: int = 16000,
        client: openai.OpenAI | None = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        if client is None:
            api_key = os.environ.get("MOONSHOT_API_KEY")
            if not api_key:
                raise LLMError("MOONSHOT_API_KEY is not set")
            client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self.client = client

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        context: str = "",
        schema: type[BaseModel] | None = None,
    ) -> LLMResult:
        # Moonshot caches identical prompt prefixes automatically, so the stable parts
        # (system + context) go first and the per-job prompt last.
        system_text = system
        if context:
            system_text += "\n\n" + context
        if schema is not None:
            system_text += (
                "\n\nReply with a single JSON object matching this JSON schema, and nothing "
                "else:\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": prompt},
        ]

        usage = LLMUsage(model=self.model, input_tokens=0, output_tokens=0)
        # JSON mode guarantees valid JSON but not the schema; retry once with the error.
        for attempt in range(2):
            text = self._call(messages, json_mode=schema is not None, usage=usage)
            if schema is None:
                return LLMResult(text=text, usage=usage)
            try:
                return LLMResult(text=text, usage=usage, parsed=schema.model_validate_json(text))
            except ValidationError as exc:
                if attempt == 1:
                    raise LLMError(
                        f"{self.model} returned JSON not matching schema: {exc}", usage
                    ) from exc
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That JSON is invalid: {exc}. Reply again."},
                ]
        raise AssertionError("unreachable")

    def _call(self, messages: list[dict[str, Any]], json_mode: bool, usage: LLMUsage) -> str:
        kwargs: dict[str, Any] = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = self.client.chat.completions.create(
                model=self.model, messages=messages, max_tokens=self.max_tokens, **kwargs
            )
        except openai.APIStatusError as exc:
            raise LLMError(f"Moonshot API error {exc.status_code}: {exc.message}") from exc
        except openai.APIConnectionError as exc:
            raise LLMError(f"Moonshot API unreachable: {exc}") from exc
        except openai.OpenAIError as exc:
            raise LLMError(f"Moonshot client error: {exc}") from exc

        # Accumulate across retries so the budget sees every billed token.
        u = response.usage
        if u is not None:
            cached = _cached_tokens(u)
            usage.input_tokens += (u.prompt_tokens or 0) - cached
            usage.cache_read_tokens += cached
            usage.output_tokens += u.completion_tokens or 0

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMError(f"{self.model} hit max_tokens={self.max_tokens}", usage)
        return choice.message.content or ""


def _cached_tokens(usage: Any) -> int:
    # Moonshot has reported cache hits both as `cached_tokens` and in prompt_tokens_details.
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details else None
    if cached is None:
        cached = getattr(usage, "cached_tokens", None)
        if cached is None and getattr(usage, "model_extra", None):
            cached = usage.model_extra.get("cached_tokens")
    return int(cached or 0)
