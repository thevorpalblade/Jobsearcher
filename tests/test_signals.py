import json
from datetime import UTC, datetime, timedelta

import pytest

from jobsearcher.companies.config import Company
from jobsearcher.config import LLMConfig
from jobsearcher.llm import BudgetedLLM, BudgetTracker, LLMResult, LLMUsage
from jobsearcher.models import Job, SourceRef, make_job_id
from jobsearcher.signals.classify import (
    PROMPT_VERSION,
    NewsAssessment,
    NewsSignal,
    classify_news,
    company_prompt,
)
from jobsearcher.signals.news import NewsQueryError, fetch_news, news_query
from jobsearcher.signals.run import digest, due, fetch_all_news
from jobsearcher.store import Store

CAPIO = Company(name="Capio")
NOW = datetime.now(UTC)


def _article(n, days_ago=1):
    seen = (NOW - timedelta(days=days_ago)).strftime("%Y%m%dT%H%M%SZ")
    return {
        "url": f"https://news.se/{n}",
        "title": f"  Capio köper vårdbolag {n} ",
        "seendate": seen,
        "domain": "news.se",
    }


class FakeClient:
    def __init__(self, text):
        self.text, self.params = text, []

    def get_text(self, url, params=None):
        self.params.append(params)
        return self.text


def test_news_query_defaults_and_short_names():
    assert news_query(CAPIO) == '"Capio" sourcecountry:sweden'
    assert news_query(Company(name="SEB")) is None
    assert news_query(Company(name="SEB", news_query='"Skandinaviska Enskilda Banken"')) == (
        '"Skandinaviska Enskilda Banken"'
    )


def test_fetch_news_parses_articles_and_gdelt_errors():
    client = FakeClient(json.dumps({"articles": [_article(1), {"url": "x"}]}))
    [item] = fetch_news(client, CAPIO, days=30)
    assert item["title"] == "Capio köper vårdbolag 1"
    assert item["company"] == "capio" and item["published_at"].startswith(
        (NOW - timedelta(days=1)).date().isoformat()
    )
    assert client.params[0]["timespan"] == "30d"
    with pytest.raises(NewsQueryError, match="phrase is too short"):
        fetch_news(FakeClient("The specified phrase is too short."), CAPIO, days=30)
    assert fetch_news(FakeClient("{}"), CAPIO, days=30) == []


def test_fetch_all_news_dedupes_and_reports_failures():
    store = Store(":memory:")
    client = FakeClient(json.dumps({"articles": [_article(1), _article(2)]}))
    report = fetch_all_news(store, client, [CAPIO, Company(name="SEB")], days=30)
    assert (report.new_items, report.failed) == (2, ["SEB"])
    assert fetch_all_news(store, client, [CAPIO], days=30).new_items == 0  # already stored


class FakeLLM:
    model = "glm"

    def __init__(self):
        self.prompts = []

    def complete(self, *, system, prompt, context="", schema=None):
        self.prompts.append(prompt)
        n = sum(1 for line in prompt.splitlines() if line[:1].isdigit())
        parsed = NewsAssessment(
            signals=[
                NewsSignal(index=i, kind="merger_acquisition", relevance=90 - i, summary=f"s{i}")
                for i in range(1, n + 1)
            ]
            + [NewsSignal(index=99, kind="other", relevance=1, summary="out of range")]
        )
        usage = LLMUsage(model="glm", input_tokens=10, output_tokens=10, billed=False)
        return LLMResult(text=parsed.model_dump_json(), usage=usage, parsed=parsed)


def test_classify_and_digest():
    store = Store(":memory:")
    fetch_all_news(
        store, FakeClient(json.dumps({"articles": [_article(1), _article(2, 60)]})), [CAPIO], 90
    )
    store.upsert_job(
        Job(
            id=make_job_id("x", "1"),
            title="HRBP",
            company="Capio",
            sources=[SourceRef(source="x", source_id="1")],
        )
    )
    llm = FakeLLM()
    budgeted = BudgetedLLM(llm, BudgetTracker(store, LLMConfig()), "ranking")
    report = classify_news(store, budgeted, [CAPIO], context="CV", max_parallel=2)
    assert (report.items, report.classified) == (2, 2)
    assert "1. [" in llm.prompts[0] and "Capio köper" in llm.prompts[0]
    assert store.unclassified_news(PROMPT_VERSION) == []
    assert len(store.unclassified_news("next")) == 2  # a prompt change re-classifies

    [entry] = digest(store, [CAPIO], days=30)  # the 60-day-old item is outside the window
    assert (entry.score, entry.open_jobs, len(entry.signals)) == (
        88,
        1,
        1,
    )  # headlines oldest first
    assert digest(store, [CAPIO], days=30, min_relevance=95) == []


def test_company_prompt_numbers_headlines():
    rows = [{"published_at": "2026-09-01T00:00:00", "domain": "di.se", "title": "Capio växer"}]
    assert company_prompt(CAPIO, rows).splitlines()[-1] == "1. [2026-09-01, di.se] Capio växer"


def test_signals_due_weekly():
    store = Store(":memory:")
    assert due(store, 7)
    store.set_last_run("signals", NOW - timedelta(days=3))
    assert not due(store, 7)
    assert due(store, 7, now=NOW + timedelta(days=5))


GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>Capio köper vårdbolag - Dagens industri</title>
<link>https://news.google.com/rss/articles/abc</link>
<pubDate>Wed, 30 Sep 2026 07:56:43 GMT</pubDate>
<source url="https://www.di.se">Dagens industri</source></item>
<item><title></title><link>https://news.google.com/rss/articles/empty</link></item>
</channel></rss>"""


def test_google_news_parsing_and_query():
    from jobsearcher.signals.news import fetch_google_news, google_query

    client = FakeClient(GOOGLE_RSS)
    [item] = fetch_google_news(client, CAPIO, days=30)
    assert item["title"] == "Capio köper vårdbolag"  # publisher suffix dropped
    assert item["domain"] == "Dagens industri"
    assert item["published_at"].startswith("2026-09-30T07:56:43")
    assert client.params[0]["q"] == '"Capio" when:30d'
    seb = Company(name="SEB", news_query='"Skandinaviska Enskilda Banken" sourcecountry:sweden')
    assert google_query(seb, 7) == '"Skandinaviska Enskilda Banken" when:7d'
    with pytest.raises(NewsQueryError, match="not RSS"):
        fetch_google_news(FakeClient("<html>blocked</html"), CAPIO, days=30)


def test_news_source_follows_robots_setting():
    from jobsearcher.config import Config, CrawlConfig

    config = Config()
    assert config.news_source == "gdelt"
    config.crawl = CrawlConfig(respect_robots=False)
    assert config.news_source == "google_news"
    config.companies.news_source = "gdelt"
    assert config.news_source == "gdelt"
