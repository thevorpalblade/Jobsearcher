import json
import subprocess

import pytest
from pydantic import BaseModel

from jobsearcher.config import LLMConfig
from jobsearcher.llm import BudgetedLLM, BudgetTracker, LLMError
from jobsearcher.llm.claude_code_client import ClaudeCodeLLM
from jobsearcher.store import Store


class Pong(BaseModel):
    reply: str


# Trimmed from a real `claude -p --output-format json --json-schema ...` response.
SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": '{"reply":"pong"}',
    "structured_output": {"reply": "pong"},
    "total_cost_usd": 0.00682,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 719,
        "cache_read_input_tokens": 0,
        "output_tokens": 53,
    },
    "modelUsage": {"claude-opus-5-5": {"inputTokens": 2, "outputTokens": 53}},
}


class FakeRun:
    def __init__(self, payload, returncode=0, stdout=None):
        self.payload, self.returncode, self.stdout = payload, returncode, stdout
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        stdout = self.stdout if self.stdout is not None else json.dumps(self.payload)
        return subprocess.CompletedProcess(cmd, self.returncode, stdout, "boom")


@pytest.fixture(autouse=True)
def claude_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda exe: f"/usr/bin/{exe}")


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_structured_call_builds_expected_command():
    run = FakeRun(SUCCESS)
    llm = ClaudeCodeLLM("opus", effort="medium", run=run)
    result = llm.complete(system="rank", context="MASTER CV", prompt="job ad", schema=Pong)

    assert result.parsed == Pong(reply="pong")
    assert result.usage.model == "claude-code/claude-opus-5-5"
    assert result.usage.billed is False
    assert result.usage.cache_write_tokens == 719

    cmd, kwargs = run.calls[0]
    assert "-p" in cmd and "--bare" not in cmd  # --bare would ignore the subscription login
    assert _flag(cmd, "--output-format") == "json"
    assert _flag(cmd, "--system-prompt") == "rank\n\nMASTER CV"
    assert _flag(cmd, "--tools") == ""
    assert _flag(cmd, "--effort") == "medium"
    assert json.loads(_flag(cmd, "--json-schema"))["required"] == ["reply"]
    assert kwargs["input"] == "job ad"


def test_error_result_raises_with_usage():
    run = FakeRun({**SUCCESS, "is_error": True, "result": "Claude usage limit reached"}, 1)
    with pytest.raises(LLMError, match="usage limit") as exc:
        ClaudeCodeLLM("opus", run=run).complete(system="s", prompt="p")
    assert exc.value.usage is not None


def test_non_json_output_raises():
    run = FakeRun(None, returncode=1, stdout="")
    with pytest.raises(LLMError, match="exit 1"):
        ClaudeCodeLLM("opus", run=run).complete(system="s", prompt="p")


def test_missing_cli(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda exe: None)
    llm = ClaudeCodeLLM("opus")
    with pytest.raises(LLMError, match="not found"):
        llm.complete(system="s", prompt="p")


def test_subscription_calls_bypass_dollar_budget():
    store = Store(":memory:")
    tracker = BudgetTracker(store, LLMConfig(monthly_budget_usd=0))  # budget exhausted
    llm = BudgetedLLM(ClaudeCodeLLM("opus", run=FakeRun(SUCCESS)), tracker, "drafting")
    llm.complete(system="s", prompt="p", schema=Pong)
    assert tracker.month_to_date() == 0
    assert tracker.subscription_usage() == (1, 2 + 53 + 719)
