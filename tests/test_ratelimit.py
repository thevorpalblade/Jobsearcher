import httpx
import openai
import pytest
from pydantic import BaseModel

from jobsearcher.llm.base import LLMError
from jobsearcher.llm.openai_compatible import OpenAICompatibleLLM
from jobsearcher.llm.ratelimit import MAX_COOLDOWN_S, RateLimiter, limiter_for


class Clock:
    """A fake clock whose sleep() just moves time forward."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def limiter(rpm=30, clock=None):
    clock = clock or Clock()
    return RateLimiter(rpm, clock=clock, sleep=clock.sleep), clock


def test_requests_are_spaced_evenly():
    lim, clock = limiter(30)  # one every 2 s
    waits = [lim.acquire() for _ in range(4)]
    assert waits == [0.0, 2.0, 2.0, 2.0]
    clock.now += 60  # idle for a while: no backlog, the next one goes straight out
    assert lim.acquire() == 0.0


def test_a_429_pauses_everyone_with_a_growing_cooldown():
    lim, clock = limiter(60)
    lim.acquire()
    assert lim.too_many_requests() == 30 and lim.cooling_down
    assert lim.acquire() == pytest.approx(30)  # the next caller waits out the cooldown
    assert lim.too_many_requests() == 60  # another 429 in a row: doubled
    assert [lim.too_many_requests() for _ in range(6)][-1] == MAX_COOLDOWN_S  # capped
    lim.succeeded()
    assert lim.too_many_requests() == 30  # a success starts the streak over
    assert lim.too_many_requests(retry_after=7) == 7  # the server's own advice wins


def test_one_limiter_per_api_and_none_when_off():
    assert limiter_for("x-api", 0) is None
    first = limiter_for("x-api", 30)
    assert limiter_for("x-api", 30) is first
    assert limiter_for("x-api", 20) is not first  # a changed limit makes a new one


class Pong(BaseModel):
    reply: str


def client_with(statuses, lim=None, max_retries=2, retry_after=None):
    """An LLM client whose server answers with `statuses` in turn (200 = a valid reply)."""
    sleeps, seen = [], []
    queue = list(statuses)

    def handler(request):
        status = queue.pop(0)
        seen.append(status)
        if status == 200:
            body = {
                "id": "c", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": '{"reply": "pong"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            }  # fmt: skip
            return httpx.Response(200, json=body)
        headers = {"retry-after": str(retry_after)} if retry_after else {}
        return httpx.Response(status, json={"error": "x"}, headers=headers)

    sdk = openai.OpenAI(
        api_key="k", base_url="https://api.example/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )  # fmt: skip
    llm = OpenAICompatibleLLM(
        "m", client=sdk, max_retries=max_retries, limiter=lim, sleep=sleeps.append
    )
    return llm, seen, sleeps


def ask(llm):
    return llm.complete(system="s", prompt="p", schema=Pong)


def test_a_429_is_retried_after_the_cooldown_not_immediately():
    lim, clock = limiter(60)
    llm, seen, sleeps = client_with([429, 200], lim)
    assert ask(llm).parsed.reply == "pong"
    assert seen == [429, 200]
    # The retry waited out the 30 s cooldown through the limiter (not a hidden SDK retry).
    assert clock.sleeps == [pytest.approx(30)] and not lim.cooling_down and lim._streak == 0


def test_persistent_429s_give_up_after_the_retries_and_keep_everyone_cooling():
    lim, clock = limiter(60)
    llm, seen, _ = client_with([429, 429, 429, 200], lim, max_retries=2)
    with pytest.raises(LLMError, match="429"):
        ask(llm)
    assert seen == [429, 429, 429]  # three attempts, no more
    assert clock.sleeps == [pytest.approx(30), pytest.approx(60)]  # growing cooldowns
    assert lim.cooling_down  # the next caller, in any thread, also waits


def test_a_servers_retry_after_is_respected():
    lim, clock = limiter(60)
    llm, seen, _ = client_with([429, 200], lim, retry_after=45)
    ask(llm)
    assert clock.sleeps == [pytest.approx(45)]


def test_server_errors_are_retried_with_backoff_and_each_counts_against_the_limit():
    lim, clock = limiter(30)
    llm, seen, sleeps = client_with([504, 504, 200], lim)
    assert ask(llm).parsed.reply == "pong"
    assert seen == [504, 504, 200] and sleeps == [5.0, 15.0]  # backoff between attempts
    assert clock.sleeps == [2.0, 2.0]  # and every attempt still waited for its slot

    llm, seen, _ = client_with([504, 504, 504], None, max_retries=2)
    with pytest.raises(LLMError, match="504"):
        ask(llm)
    llm, seen, _ = client_with([400, 200], None)
    with pytest.raises(LLMError, match="400"):
        ask(llm)
    assert seen == [400]  # a client error isn't retried


def test_without_a_limiter_a_429_still_backs_off():
    llm, seen, sleeps = client_with([429, 429, 200], None)
    ask(llm)
    assert sleeps == [10.0, 20.0]
