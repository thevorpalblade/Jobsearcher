"""Claude via the Claude Code CLI (`claude -p`), billed to a Claude subscription.

Calls count against the subscription's usage limits instead of costing API money.
Authentication: `claude setup-token` prints a long-lived token; put it in .env as
CLAUDE_CODE_OAUTH_TOKEN. (`--bare` is deliberately not used: it ignores subscription
logins.)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any

from pydantic import BaseModel, ValidationError

from jobsearcher.llm.base import LLMError, LLMResult, LLMUsage

# A single command-line argument is limited to 128 KiB on Linux; the system prompt
# (instructions + CV) is passed as one.
_MAX_SYSTEM_PROMPT_BYTES = 120_000


class ClaudeCodeLLM:
    # Usage is covered by the subscription, so the monthly $ budget doesn't gate it.
    metered = False

    def __init__(
        self,
        model: str,
        effort: str | None = None,
        timeout_s: float = 600,
        executable: str = "claude",
        run: Any = subprocess.run,
    ):
        self.model = model
        self.effort = effort
        self.timeout_s = timeout_s
        self.executable = executable
        self._run = run

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        context: str = "",
        schema: type[BaseModel] | None = None,
    ) -> LLMResult:
        system_prompt = f"{system}\n\n{context}" if context else system
        if len(system_prompt.encode()) > _MAX_SYSTEM_PROMPT_BYTES:
            raise LLMError("System prompt + context too large for the Claude Code CLI")

        exe = shutil.which(self.executable)
        if exe is None:
            raise LLMError(f"Claude Code CLI ({self.executable!r}) not found on PATH")

        cmd = [
            exe,
            "-p",
            "--output-format", "json",
            "--model", self.model,
            "--system-prompt", system_prompt,
            "--tools", "",  # plain text generation; no file or shell access
            "--strict-mcp-config",
            "--no-session-persistence",
        ]  # fmt: skip
        if self.effort:
            cmd += ["--effort", self.effort]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema.model_json_schema())]

        # Run from an empty directory so no project CLAUDE.md or settings get loaded.
        with tempfile.TemporaryDirectory(prefix="jobsearcher-claude-") as cwd:
            try:
                proc = self._run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    cwd=cwd,
                    env=os.environ.copy(),
                )
            except subprocess.TimeoutExpired as exc:
                raise LLMError(f"Claude Code timed out after {self.timeout_s:.0f}s") from exc

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            raise LLMError(f"Claude Code failed (exit {proc.returncode}): {detail}") from exc

        usage = _usage(data, self.model)
        if data.get("is_error") or proc.returncode != 0:
            # Covers auth failures and hitting the subscription's usage limit.
            message = data.get("result") or data.get("subtype") or "unknown error"
            raise LLMError(f"Claude Code error: {message}", usage)

        if schema is None:
            return LLMResult(text=data.get("result") or "", usage=usage)
        structured = data.get("structured_output")
        try:
            parsed = schema.model_validate(structured)
        except ValidationError as exc:
            raise LLMError(f"Claude Code returned invalid structured output: {exc}", usage) from exc
        return LLMResult(
            text=json.dumps(structured, ensure_ascii=False), usage=usage, parsed=parsed
        )


def _usage(data: dict[str, Any], model: str) -> LLMUsage:
    u = data.get("usage") or {}
    # `modelUsage` is keyed by the full model ID that served the request.
    served = next(iter(data.get("modelUsage") or {}), model)
    return LLMUsage(
        model=f"claude-code/{served}",
        input_tokens=int(u.get("input_tokens") or 0),
        output_tokens=int(u.get("output_tokens") or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
        billed=False,
    )
