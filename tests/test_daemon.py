import argparse
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from jobsearcher import cli
from jobsearcher.config import Config, ScheduleConfig
from jobsearcher.llm import LLMError
from jobsearcher.signals import classify
from jobsearcher.store import Store

TZ = ZoneInfo("Europe/Stockholm")
ARGS = argparse.Namespace(config=None, no_initial_run=False)


class Script:
    """Replaces the pipeline commands: each returns its next prepared exit status."""

    def __init__(self, monkeypatch, search=0, rank=(0,), signals=(0,), until=10**6):
        self.calls, self.sleeps = [], []
        self.rank, self.signals = list(rank), list(signals)

        def make(name, queue=None, fixed=0):
            def command(config, args):
                self.calls.append(name)
                return queue.pop(0) if queue is not None else fixed

            return command

        monkeypatch.setattr(cli, "cmd_search", make("search", fixed=search))
        monkeypatch.setattr(cli, "cmd_rank", make("rank", self.rank))
        monkeypatch.setattr(cli, "cmd_signals", make("signals", self.signals))
        monkeypatch.setattr(cli, "load_config", lambda path=None: self.config)
        monkeypatch.setattr(cli, "seconds_until", lambda daily_at, tz, now=None: until)
        self.config = Config()
        self.config.sources.companies = True
        self.config.companies_config = self.config.companies_config.parent / "companies.yaml"
        self.config.companies_config.write_text("companies: []\n")  # the cwd is a temp dir
        self.config.schedule = ScheduleConfig(retry_minutes=30, retries=3)

    def run(self):
        cli.daemon_cycle(self.config, ARGS, TZ, sleep=self.sleeps.append)
        return self.calls


def test_a_stalled_ranking_is_retried_until_it_finishes(monkeypatch):
    script = Script(monkeypatch, rank=(cli.RETRY, cli.RETRY, 0), signals=(0, 0, 0))
    # One pass (search, rank, signals), then retries: rank + leftover classification only.
    assert script.run() == ["search", "rank", "signals", "rank", "signals", "rank", "signals"]
    assert script.sleeps == [1800, 1800]  # 30 minutes between attempts


def test_retries_are_capped_and_never_search_again(monkeypatch):
    script = Script(monkeypatch, rank=(cli.RETRY,) * 5, signals=(0,) * 5)
    calls = script.run()
    assert calls.count("rank") == 4 and calls.count("search") == 1  # the pass + 3 retries
    assert len(script.sleeps) == 3


def test_news_classification_stalling_also_retries(monkeypatch):
    script = Script(monkeypatch, rank=(0, 0), signals=(cli.RETRY, 0))
    assert script.run() == ["search", "rank", "signals", "rank", "signals"]


def test_no_retry_when_nothing_stalled_or_when_switched_off(monkeypatch):
    assert Script(monkeypatch, rank=(0,), signals=(0,)).run() == ["search", "rank", "signals"]
    script = Script(monkeypatch, rank=(cli.RETRY,), signals=(0,))
    script.config.schedule.retries = 0
    assert script.run() == ["search", "rank", "signals"] and script.sleeps == []
    script = Script(monkeypatch, rank=(cli.RETRY,), signals=(0,))
    script.config.schedule.retry_minutes = 0
    assert script.run() == ["search", "rank", "signals"] and script.sleeps == []


def test_retries_stop_when_the_next_daily_run_is_close(monkeypatch):
    script = Script(monkeypatch, rank=(cli.RETRY, 0), signals=(0, 0), until=20 * 60)
    assert script.run() == ["search", "rank", "signals"]  # the daily run is 20 min away
    assert script.sleeps == []


def test_a_crashing_retry_ends_the_loop_without_taking_the_daemon_down(monkeypatch):
    script = Script(monkeypatch, rank=(cli.RETRY,), signals=(0,))

    def boom(config, args):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cli, "retry_stalled", boom)
    script.run()  # no exception escapes
    assert script.sleeps == [1800]


def test_a_failed_search_stops_the_pass(monkeypatch):
    script = Script(monkeypatch, search=2)
    assert script.run() == ["search"]


def test_the_exit_status_ignores_a_stall(monkeypatch):
    Script(monkeypatch, rank=(cli.RETRY,), signals=(cli.RETRY,))
    assert cli.run_pipeline(Config(), ARGS) == (0, True)
    Script(monkeypatch, search=1, rank=(0,), signals=(0,))
    assert cli.run_pipeline(Config(), ARGS) == (1, False)


# --- news classification stops on a failure streak ---------------------------------------


class FailingLLM:
    model = "glm"

    def __init__(self, fail_first):
        self.calls, self.fail_first = 0, fail_first

    def complete(self, **kwargs):
        raise NotImplementedError  # classification uses check/call/record

    def check(self):
        pass

    def call(self, **kwargs):
        self.calls += 1
        raise LLMError("429 rate limited")

    def record(self, usage):
        pass


def test_classification_stops_after_a_streak_of_failed_batches(monkeypatch):
    from jobsearcher.companies.config import Company

    store = Store(":memory:")
    now = datetime.now(UTC).isoformat()
    companies = [Company(name=f"Firm {n}") for n in range(12)]
    store.save_news_items(
        [{"id": f"n{n}", "company": c.slug, "title": f"{c.name} news", "url": f"https://n/{n}",
          "domain": "x.se", "published_at": now, "fetched_at": now}
         for n, c in enumerate(companies)]
    )  # fmt: skip
    llm = FailingLLM(0)
    report = classify.classify_news(store, llm, companies, context="CV", max_parallel=1)
    assert report.stopped_on_failures and llm.calls == classify.STOP_AFTER_FAILURES
    assert report.failed_batches == 5 and "5 failed batches in a row" in report.stopped_reason
    assert len(store.unclassified_news(classify.PROMPT_VERSION)) == 12  # all wait for the retry


def test_rank_returns_the_retry_status_after_a_failure_streak(monkeypatch, tmp_path):
    from jobsearcher import llm, ranking
    from jobsearcher.ranking.ranker import RankReport

    (tmp_path / "cv.md").write_text("# CV")
    (tmp_path / "ranking.yaml").write_text("target_roles: []\n")
    config = Config(
        data_dir=tmp_path / "data",
        cv_path=tmp_path / "cv.md",
        ranking_config=tmp_path / "ranking.yaml",
    )
    monkeypatch.setattr(llm, "make_llm", lambda *a, **k: object())
    for report, expected in [
        (RankReport(ranked=3, failed=10, deferred=50, stopped_on_failures=True), cli.RETRY),
        (RankReport(ranked=0, failed=2), 1),  # failures, but no streak stop: the old exit status
        (RankReport(ranked=5, deferred=9, stopped_reason="per-run limit reached"), 0),
    ]:
        monkeypatch.setattr(ranking, "run_ranking", lambda *a, _r=report, **k: _r)
        assert cli.cmd_rank(config, ARGS) == expected


def test_leftover_classification_skips_the_model_when_nothing_is_left(monkeypatch, tmp_path):
    from jobsearcher import llm

    (tmp_path / "companies.yaml").write_text("companies: [{name: Acme}]\n")
    config = Config(data_dir=tmp_path / "data", companies_config=tmp_path / "companies.yaml")

    def no_client(*args, **kwargs):
        raise AssertionError("no model client should be built")

    monkeypatch.setattr(llm, "make_llm", no_client)
    args = argparse.Namespace(digest_only=False, days=None, min_relevance=40, limit=0, fetch=False)
    assert cli.cmd_signals(config, args) == 0


def test_a_restart_soon_after_a_search_ranks_without_searching(monkeypatch):
    from datetime import timedelta

    script = Script(monkeypatch)
    config = script.config
    now = datetime(2026, 10, 8, 12, tzinfo=UTC)
    assert cli.search_due(config, now)  # never searched
    Store(config.db_path).set_last_run("linkedin", now - timedelta(hours=2))
    assert not cli.search_due(config, now)
    assert cli.search_due(config, now + timedelta(hours=4, minutes=1))  # 6 h by default
    cli.daemon_cycle(config, ARGS, TZ, sleep=script.sleeps.append, search=False)
    assert script.calls == ["rank", "signals"]  # no search
