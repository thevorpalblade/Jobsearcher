import json
import os
from datetime import UTC, datetime

import anthropic
import httpx
import httpx2
import openai
import pytest
from pydantic import BaseModel

from jobsearcher.config import LLMConfig
from jobsearcher.llm import BudgetedLLM, BudgetExceeded, BudgetTracker, LLMRefusal, LLMUsage
from jobsearcher.llm.anthropic_client import AnthropicLLM
from jobsearcher.llm.openai_compatible import OpenAICompatibleLLM
from jobsearcher.store import Store


class Score(BaseModel):
    fit: int
    reason: str


# --- Anthropic --------------------------------------------------------------


def _anthropic(responses):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, json=responses.pop(0))

    client = anthropic.Anthropic(
        api_key="test",
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return client, requests


def _claude_msg(text, model="claude-opus-5-5", stop_reason="end_turn", **usage):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20, **usage},
    }


def test_anthropic_structured_output_caching_and_fallback():
    client, requests = _anthropic(
        [_claude_msg('{"fit": 80, "reason": "good"}', cache_read_input_tokens=3000)]
    )
    llm = AnthropicLLM("claude-opus-5-5", effort="medium", client=client)
    result = llm.complete(system="rank", context="MASTER CV", prompt="job ad", schema=Score)

    assert result.parsed == Score(fit=80, reason="good")
    assert result.usage.cache_read_tokens == 3000
    body = json.loads(requests[0].content)
    assert body["system"][1] == {
        "type": "text",
        "text": "MASTER CV",
        "cache_control": {"type": "ephemeral"},
    }
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "medium"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback" in requests[0].headers["anthropic-beta"]


def test_anthropic_haiku_has_no_fallback_or_effort():
    client, requests = _anthropic([_claude_msg("hi", model="claude-haiku-4-5")])
    AnthropicLLM("claude-haiku-4-5", client=client).complete(system="s", prompt="p")
    body = json.loads(requests[0].content)
    assert "fallbacks" not in body and "output_config" not in body


def test_anthropic_refusal_carries_usage():
    client, _ = _anthropic([_claude_msg("", stop_reason="refusal")])
    with pytest.raises(LLMRefusal) as exc:
        AnthropicLLM("claude-opus-5-5", client=client).complete(system="s", prompt="p")
    assert exc.value.usage.input_tokens == 100


# --- Moonshot ---------------------------------------------------------------


def _moonshot(contents, cached=0):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "kimi-k2.6",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": contents.pop(0)},
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "total_tokens": 1050,
                    "cached_tokens": cached,
                },
            },
        )

    client = openai.OpenAI(
        api_key="test",
        base_url="https://api.moonshot.ai/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return client, requests


def test_moonshot_json_mode_and_cached_tokens():
    client, requests = _moonshot(['{"fit": 70, "reason": "ok"}'], cached=800)
    result = OpenAICompatibleLLM("kimi-k2.6", client=client).complete(
        system="rank", context="MASTER CV", prompt="job ad", schema=Score
    )
    assert result.parsed.fit == 70
    assert (result.usage.input_tokens, result.usage.cache_read_tokens) == (200, 800)
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert "thinking" not in requests[0]
    assert requests[0]["messages"][0]["content"].startswith("rank\n\nMASTER CV")


def test_moonshot_retries_once_on_schema_mismatch():
    client, requests = _moonshot(['{"fit": "high"}', '{"fit": 60, "reason": "fixed"}'])
    result = OpenAICompatibleLLM("kimi-k2.6", client=client).complete(
        system="s", prompt="p", schema=Score
    )
    assert result.parsed.reason == "fixed"
    assert len(requests) == 2
    assert result.usage.input_tokens == 2000  # both attempts billed


# --- Budget -----------------------------------------------------------------


class FakeLLM:
    model = "claude-opus-5-5"

    def complete(self, **kwargs):
        from jobsearcher.llm import LLMResult

        usage = LLMUsage(model=self.model, input_tokens=1_000_000, output_tokens=0)
        return LLMResult(text="x", usage=usage)


def test_cost_uses_price_table():
    tracker = BudgetTracker(Store(":memory:"), LLMConfig())
    usage = LLMUsage(
        model="claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert tracker.cost(usage) == pytest.approx(4 + 20 + 0.2 + 5)
    # Kimi doesn't bill cache writes separately; unknown models are priced high.
    assert tracker.cost(LLMUsage("kimi-k2.6", 1_000_000, 0)) == pytest.approx(0.95)
    assert tracker.cost(LLMUsage("mystery", 1_000_000, 0)) >= 5


def test_budget_pauses_drafting_before_ranking():
    store = Store(":memory:")
    tracker = BudgetTracker(store, LLMConfig(monthly_budget_usd=10))
    drafting = BudgetedLLM(FakeLLM(), tracker, "drafting")  # $4 per call
    ranking = BudgetedLLM(FakeLLM(), tracker, "ranking")

    drafting.complete(system="s", prompt="p")
    drafting.complete(system="s", prompt="p")
    assert tracker.month_to_date() == pytest.approx(8)
    with pytest.raises(BudgetExceeded):
        drafting.complete(system="s", prompt="p")  # 80% of $10 reached
    ranking.complete(system="s", prompt="p")  # ranking continues up to 100%
    with pytest.raises(BudgetExceeded):
        ranking.complete(system="s", prompt="p")


def test_month_to_date_ignores_last_month():
    store = Store(":memory:")
    store.record_llm_usage(datetime(2026, 8, 31, tzinfo=UTC), "m", "ranking", 1, 1, 0, 0, 5.0)
    store.record_llm_usage(datetime(2026, 9, 2, tzinfo=UTC), "m", "ranking", 1, 1, 0, 0, 1.5)
    tracker = BudgetTracker(store, LLMConfig())
    assert tracker.month_to_date(now=datetime(2026, 9, 29, tzinfo=UTC)) == pytest.approx(1.5)


def test_load_env_file(tmp_path, monkeypatch):
    from jobsearcher.config import load_env_file

    (tmp_path / ".env").write_text("# comment\nNEW_KEY='abc'\nSET_KEY=from-file\nEMPTY=\n")
    monkeypatch.delenv("NEW_KEY", raising=False)
    monkeypatch.delenv("EMPTY", raising=False)
    monkeypatch.setenv("SET_KEY", "from-env")
    load_env_file(tmp_path / ".env")
    assert os.environ["NEW_KEY"] == "abc"
    assert os.environ["SET_KEY"] == "from-env"
    assert "EMPTY" not in os.environ
    monkeypatch.delenv("NEW_KEY")


def test_make_llm_nvidia(monkeypatch):
    from jobsearcher.config import Config, ModelRole, Provider
    from jobsearcher.llm import make_llm

    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    config = Config()
    config.llm.ranking = ModelRole(provider=Provider.NVIDIA, model="z-ai/glm-5.3-flash")
    config.llm.ranking.extra_body = {"thinking": {"type": "disabled"}}
    llm = make_llm(config, "ranking", tracker=None)
    assert llm.client.extra_body == {"thinking": {"type": "disabled"}}
    assert str(llm.client.client.base_url).startswith("https://integrate.api.nvidia.com/v1")
    assert llm.client.label == "NVIDIA"


def test_make_llm_zai(monkeypatch):
    from jobsearcher.config import Config, ModelRole, Provider
    from jobsearcher.llm import make_llm

    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    config = Config()
    config.llm.ranking = ModelRole(provider=Provider.ZAI, model="glm-5.3-flash")
    llm = make_llm(config, "ranking", tracker=None)
    assert str(llm.client.client.base_url).startswith("https://api.z.ai/api/paas/v4")
    assert llm.client.label == "Z.ai" and llm.client.billed
    assert config.llm.price_for("glm-5.3-flash").output == 0.50


def test_make_llm_ollama_needs_no_key(monkeypatch):
    from jobsearcher.config import Config, ModelRole, Provider
    from jobsearcher.llm import make_llm

    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    config = Config()
    config.llm.ranking = ModelRole(provider=Provider.OLLAMA, model="qwen3:8b")
    llm = make_llm(config, "ranking", tracker=None)
    assert str(llm.client.client.base_url).startswith("http://localhost:11434/v1")
    assert llm.client.label == "Ollama" and llm.client.limiter is None
    assert not llm.client.billed  # local: never counted against the budget


def test_moonshot_limiter_is_opt_in(monkeypatch):
    from jobsearcher.config import Config, ModelRole, Provider
    from jobsearcher.llm import make_llm

    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
    config = Config()
    config.llm.ranking = ModelRole(provider=Provider.MOONSHOT, model="kimi-k2.6")
    assert make_llm(config, "ranking", tracker=None).client.limiter is None
    config.llm.moonshot_requests_per_minute = 3
    assert make_llm(config, "ranking", tracker=None).client.limiter is not None


def test_extra_body_is_sent():
    client, requests = _moonshot(['{"fit": 70, "reason": "ok"}'])
    llm = OpenAICompatibleLLM(
        "z-ai/glm-5.3-flash", client=client, extra_body={"thinking": {"type": "disabled"}}
    )
    llm.complete(system="s", prompt="p", schema=Score)
    assert requests[0]["thinking"] == {"type": "disabled"}


def test_enforce_schema_sends_json_schema_response_format():
    client, requests = _moonshot(['{"fit": 70, "reason": "ok"}'])
    llm = OpenAICompatibleLLM("z-ai/glm-5.3-flash", client=client, enforce_schema=True)
    assert llm.complete(system="s", prompt="p", schema=Score).parsed.fit == 70
    fmt = requests[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "Score"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["properties"].keys() == {"fit", "reason"}


def test_grounding_models_default_to_a_quick_ranking_model(monkeypatch):
    from jobsearcher.config import Config, ModelRole, Provider
    from jobsearcher.llm import BudgetTracker, LLMError, make_llm
    from jobsearcher.store import Store

    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    config = Config()
    config.llm.ranking = ModelRole(provider=Provider.NVIDIA, model="z-ai/glm-5.3-flash")
    tracker = BudgetTracker(Store(":memory:"), config.llm)

    check = make_llm(config, "grounding", tracker)
    assert check.model == "z-ai/glm-5.3-flash" and check.purpose == "grounding"
    # The SDK itself never retries (we do, rate limited); the grounding check is quick.
    assert (check.client.client.timeout, check.client.client.max_retries) == (150, 0)
    assert check.client.max_retries == 0 and check.client.limiter is not None
    ranking = make_llm(config, "ranking", tracker).client
    assert (ranking.client.timeout, ranking.client.max_retries, ranking.max_retries) == (600, 0, 2)
    assert ranking.limiter is check.client.limiter  # one limiter for all NVIDIA calls
    config.llm.nvidia_requests_per_minute = 0
    assert make_llm(config, "ranking", tracker).client.limiter is None

    with pytest.raises(LLMError, match="No grounding fallback"):
        make_llm(config, "grounding_fallback", tracker)
    config.llm.grounding_fallback = ModelRole(provider=Provider.CLAUDE_CODE, model="haiku")
    assert make_llm(config, "grounding_fallback", tracker).model == "haiku"
    config.llm.grounding = ModelRole(provider=Provider.NVIDIA, model="other", timeout_s=30)
    assert make_llm(config, "grounding", tracker).client.client.timeout == 30


def test_make_llm_gemini(monkeypatch):
    from jobsearcher.config import Config, ModelRole, Provider
    from jobsearcher.llm import make_llm

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    config = Config()
    config.llm.ranking = ModelRole(provider=Provider.GEMINI, model="gemini-flash-latest")
    llm = make_llm(config, "ranking", tracker=None)
    assert str(llm.client.client.base_url).startswith("https://generativelanguage.googleapis.com/")
    assert llm.client.label == "Gemini" and llm.client.limiter is not None  # free-tier pace
