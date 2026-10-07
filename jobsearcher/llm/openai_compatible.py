"""Providers with an OpenAI-compatible chat API: Kimi via Moonshot (MOONSHOT_API_KEY)
and models hosted on NVIDIA's serverless endpoints, e.g. GLM (NVIDIA_API_KEY)."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

import openai
from pydantic import BaseModel, ValidationError

from jobsearcher.llm.base import LLMError, LLMResult, LLMUsage
from jobsearcher.llm.ratelimit import RateLimiter

log = logging.getLogger(__name__)


class OpenAICompatibleLLM:
    def __init__(
        self,
        model: str,
        base_url: str = "https://api.moonshot.ai/v1",
        api_key_env: str | None = "MOONSHOT_API_KEY",  # None: the server needs no key (Ollama)
        label: str = "Moonshot",
        max_tokens: int = 16000,
        extra_body: dict[str, Any] | None = None,
        enforce_schema: bool = False,
        timeout_s: float | None = None,
        max_retries: int = 2,
        limiter: RateLimiter | None = None,
        billed: bool = True,  # False for a local server (Ollama): its tokens cost nothing
        sleep: Callable[[float], None] = time.sleep,
        client: openai.OpenAI | None = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}
        # Send the schema as `response_format: json_schema` so the server constrains
        # the output to it, instead of plain JSON mode (which only guarantees JSON).
        self.enforce_schema = enforce_schema
        self.label = label  # provider name for error messages
        self.billed = billed
        # Retries are done here, not by the SDK, so each attempt is rate limited and a 429
        # pauses every caller (see ratelimit.py).
        self.max_retries = max_retries
        self.limiter = limiter
        self._sleep = sleep
        if client is None:
            api_key = os.environ.get(api_key_env) if api_key_env else "none"
            if not api_key:
                raise LLMError(f"{api_key_env} is not set")
            client = openai.OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_s if timeout_s is not None else openai.NOT_GIVEN,
                max_retries=0,
            )
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
                "\n\nReply with a single JSON object that fills in this JSON schema with your "
                "answer (don't repeat the schema itself), and nothing else:\n"
                + json.dumps(schema.model_json_schema(), ensure_ascii=False)
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": prompt},
        ]

        usage = LLMUsage(model=self.model, input_tokens=0, output_tokens=0, billed=self.billed)
        # JSON mode guarantees valid JSON but not the schema; retry once with the error.
        for attempt in range(2):
            text = self._call(messages, schema, usage=usage)
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

    def _call(
        self, messages: list[dict[str, Any]], schema: type[BaseModel] | None, usage: LLMUsage
    ) -> str:
        kwargs: dict[str, Any] = {}
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        if schema is not None and self.enforce_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                },
            }
        elif schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        response = self._create(messages, kwargs)

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

    def _create(self, messages: list[dict[str, Any]], kwargs: dict) -> Any:
        """One chat completion, retried on 429, 5xx and connection errors. Every attempt
        waits for the rate limiter; a 429 starts a cooldown for all callers."""
        last = self.max_retries
        for attempt in range(last + 1):
            if self.limiter:
                self.limiter.acquire()
            try:
                response = self.client.chat.completions.create(
                    model=self.model, messages=messages, max_tokens=self.max_tokens, **kwargs
                )
            except openai.RateLimitError as exc:
                pause = (
                    self.limiter.too_many_requests(_retry_after(exc))
                    if self.limiter
                    else (_retry_after(exc) or 10.0 * 2**attempt)
                )
                if attempt == last:
                    raise LLMError(
                        f"{self.label} API error 429: rate limited ({exc.message})"
                    ) from exc
                log.warning("%s rate limited (429); pausing %.0f s", self.label, pause)
                if not self.limiter:
                    self._sleep(pause)  # with a limiter, the next acquire() waits it out
            except (openai.APIConnectionError, openai.InternalServerError) as exc:
                if attempt == last:
                    if isinstance(exc, openai.InternalServerError):
                        raise LLMError(
                            f"{self.label} API error {exc.status_code}: {exc.message}"
                        ) from exc
                    raise LLMError(f"{self.label} API unreachable: {exc}") from exc
                self._sleep(min(5.0 * 3**attempt, 30.0))
            except openai.APIStatusError as exc:
                raise LLMError(f"{self.label} API error {exc.status_code}: {exc.message}") from exc
            except openai.OpenAIError as exc:
                raise LLMError(f"{self.label} client error: {exc}") from exc
            else:
                if self.limiter:
                    self.limiter.succeeded()
                return response
        raise AssertionError("unreachable")


def _retry_after(exc: openai.APIStatusError) -> float | None:
    try:
        return float(exc.response.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return None


def _cached_tokens(usage: Any) -> int:
    # Moonshot has reported cache hits both as `cached_tokens` and in prompt_tokens_details.
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details else None
    if cached is None:
        cached = getattr(usage, "cached_tokens", None)
        if cached is None and getattr(usage, "model_extra", None):
            cached = usage.model_extra.get("cached_tokens")
    return int(cached or 0)
