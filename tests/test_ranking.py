import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import make_assessment, make_job

from jobsearcher.config import LLMConfig
from jobsearcher.llm import BudgetedLLM, BudgetTracker, LLMError, LLMResult, LLMUsage
from jobsearcher.models import Contact
from jobsearcher.ranking import final_score, load_ranking_config, ranked_jobs, run_ranking
from jobsearcher.ranking.config import RankingConfig, TargetRole, Weights
from jobsearcher.ranking.prefilter import matched_roles, prefilter_status, select_for_ranking
from jobsearcher.ranking.ranker import build_context, score_breakdown
from jobsearcher.store import Store

EXAMPLE = Path(__file__).parent.parent / "ranking.example.yaml"
CV = "# Anna Andersson\nHR Business Partner at Exempel AB, 2019-2026. Led a reorganisation."


_job = make_job
_assessment = make_assessment


class FakeLLM:
    model = "kimi-k2.6"

    def __init__(self, assessments=None, fail_titles=()):
        self.assessments = assessments or {}
        self.fail_titles = set(fail_titles)
        self.prompts = []

    def complete(self, *, system, prompt, context="", schema=None):
        self.prompts.append((context, prompt))
        title = prompt.splitlines()[0].removeprefix("Job title: ")
        usage = LLMUsage(model=self.model, input_tokens=1000, output_tokens=200)
        if title in self.fail_titles:
            raise LLMError("bad output", usage)
        parsed = self.assessments.get(title, _assessment())
        return LLMResult(text=parsed.model_dump_json(), usage=usage, parsed=parsed)


def _setup(jobs, llm=None, config=None, budget=20.0):
    store = Store(":memory:")
    for job in jobs:
        store.upsert_job(job)
    llm = llm or FakeLLM()
    tracker = BudgetTracker(store, LLMConfig(monthly_budget_usd=budget))
    return store, BudgetedLLM(llm, tracker, "ranking"), llm, config or load_ranking_config(EXAMPLE)


def test_example_config_loads_with_requested_roles():
    config = load_ranking_config(EXAMPLE)
    names = [r.name for r in config.target_roles]
    assert names == [
        "HR Business Partner",
        "Change management",
        "Operations manager",
        "Project manager",
    ]
    assert "projektledare" in config.role_terms


def test_prefilter_whole_word_matching_and_order():
    config = load_ranking_config(EXAMPLE)
    in_title = _job(1, "Projektledare IT", published_day=1)
    in_text = _job(2, "Konsult", "Du blir vår nya förändringsledare.", published_day=5)
    none = _job(3, "Lagerarbetare", "Truckkort krävs.", published_day=9)
    partial = _job(4, "HRBPX-specialist")  # alias only as part of another word
    assert matched_roles(in_title, config) == (["Project manager"], True)
    assert matched_roles(partial, config) == ([], False)
    assert select_for_ranking([none, in_text, in_title, partial], config) == [in_title, in_text]


def test_run_ranking_scores_caches_and_adds_contacts():
    job = _job(1, "HR Business Partner")
    contact = {"name": "Per Persson", "role": "HR-chef", "email": None, "phone": None}
    llm = FakeLLM({"HR Business Partner": _assessment(90, 60, [contact])})
    store, budgeted, _, config = _setup([job], llm)

    report = run_ranking(store, budgeted, config, CV)
    assert (report.ranked, report.cached) == (1, 0)
    [(ranked_job, ranking, score)] = ranked_jobs(store, config)
    assert score == round(90 * 0.6 + 60 * 0.4)
    assert ranking.assessment.fit_score == 90
    assert Contact(name="Per Persson", role="HR-chef", provenance="llm:ad_text") in (
        ranked_job.contacts
    )

    # Second run: nothing changed, so nothing is sent to the LLM.
    report = run_ranking(store, budgeted, config, CV)
    assert (report.ranked, report.cached) == (0, 1)
    assert len(llm.prompts) == 1

    # A changed CV invalidates the cached ranking.
    run_ranking(store, budgeted, config, CV + "\nNew certification.")
    assert len(llm.prompts) == 2


def test_context_contains_roles_preferences_and_cv():
    context = build_context(CV, load_ranking_config(EXAMPLE))
    assert "HR Business Partner" in context and "HRBP" in context
    assert "Dealbreakers:" in context
    config = load_ranking_config(EXAMPLE)
    assert config.preferences.situation.startswith("Lives in Stockholm")
    assert "Situation: Lives in Stockholm" in build_context(CV, config)
    assert context.endswith(CV)


def test_per_run_cap_defers_the_rest():
    jobs = [_job(i, f"Projektledare {i}", published_day=i) for i in range(1, 6)]
    config = load_ranking_config(EXAMPLE)
    config.prefilter.max_llm_calls_per_run = 2
    store, budgeted, llm, _ = _setup(jobs, config=config)
    report = run_ranking(store, budgeted, config, CV)
    assert (report.ranked, report.deferred) == (2, 3)
    assert report.stopped_reason == "per-run limit reached"


def test_budget_exhaustion_stops_ranking():
    jobs = [_job(i, f"Projektledare {i}") for i in range(1, 4)]
    store, budgeted, llm, config = _setup(jobs, budget=0)
    report = run_ranking(store, budgeted, config, CV)
    assert report.ranked == 0 and report.deferred == 3
    assert "paused" in report.stopped_reason


def test_failures_are_counted_and_retried_next_run():
    jobs = [_job(1, "Projektledare A"), _job(2, "Projektledare B")]
    llm = FakeLLM(fail_titles={"Projektledare A"})
    store, budgeted, _, config = _setup(jobs, llm)
    report = run_ranking(store, budgeted, config, CV)
    assert (report.ranked, report.failed) == (1, 1)
    llm.fail_titles.clear()
    assert run_ranking(store, budgeted, config, CV).ranked == 1


def test_weights_and_fingerprint():
    assert Weights(fit=1, success=0).combine(80, 20) == 80
    assert Weights(fit=0, success=0).combine(80, 20) == 50
    a = RankingConfig(target_roles=[TargetRole(name="A")])
    b = a.model_copy(deep=True)
    b.prefilter.max_llm_calls_per_run = 1  # limits don't invalidate rankings
    assert a.fingerprint() == b.fingerprint()
    b.target_roles.append(TargetRole(name="B"))
    assert a.fingerprint() != b.fingerprint()


def test_assessment_schema_bounds():
    with pytest.raises(ValueError):
        _assessment(fit=150)


def test_language_adjustments():
    config = load_ranking_config(EXAMPLE)  # english_ad +10, required -20, merit -5
    base = round(80 * 0.6 + 60 * 0.4)  # 72
    assert final_score(_assessment(), config) == base
    assert final_score(_assessment(language="en"), config) == base + 10
    assert final_score(_assessment(swedish="required"), config) == base - 20
    assert final_score(_assessment(language="en", swedish="merit"), config) == base + 5
    assert final_score(_assessment(fit=100, success=100, language="en"), config) == 100


def test_weight_and_adjustment_edits_apply_without_reranking():
    job = _job(1, "Projektledare")
    llm = FakeLLM({"Projektledare": _assessment(90, 50, language="en")})
    store, budgeted, _, config = _setup([job], llm)
    run_ranking(store, budgeted, config, CV)
    config.weights = Weights(fit=1, success=0)
    config.adjustments.english_ad = 0
    [(_, _, score)] = ranked_jobs(store, config)
    assert score == 90
    assert run_ranking(store, budgeted, config, CV).cached == 1
    assert len(llm.prompts) == 1


def _with_occupation(job, field, group):
    return job.model_copy(update={"occupation_field": field, "occupation_group": group})


def test_occupation_filters_per_role():
    pm = TargetRole(
        name="Project manager",
        aliases=["projektledare"],
        exclude_occupations=["bygg och anläggning", "Yrken med teknisk inriktning"],
        except_occupations=["logistik"],
    )
    hr = TargetRole(name="HR Business Partner", include_occupations=["Administration"])
    config = RankingConfig(target_roles=[pm, hr])

    construction = _with_occupation(
        _job(1, "Projektledare bygg"),
        "Bygg och anläggning",
        "Ingenjörer och tekniker inom bygg och anläggning",
    )
    it = _with_occupation(_job(2, "IT-projektledare"), "Data/IT", "Mjukvaru- och systemutvecklare")
    logistics = _with_occupation(
        _job(6, "Projektledare logistik"),
        "Yrken med teknisk inriktning",
        "Ingenjörer och tekniker inom industri, logistik och produktionsplanering",
    )
    electrical = _with_occupation(
        _job(7, "Projektledare el"),
        "Yrken med teknisk inriktning",
        "Ingenjörer och tekniker inom elektroteknik",
    )
    unknown = _job(3, "Projektledare")  # no occupation data: always passes
    hr_admin = _with_occupation(
        _job(4, "HR Business Partner"), "Administration, ekonomi, juridik", "HR-specialister"
    )
    hr_other = _with_occupation(_job(5, "HR Business Partner"), "Data/IT", None)

    assert matched_roles(construction, config) == ([], False)
    assert matched_roles(construction, config, apply_occupation_filters=False)[0] == [
        "Project manager"
    ]
    assert matched_roles(it, config)[0] == ["Project manager"]
    assert matched_roles(logistics, config)[0] == ["Project manager"]  # excepted
    assert matched_roles(electrical, config)[0] == []
    assert matched_roles(unknown, config)[0] == ["Project manager"]
    assert matched_roles(hr_admin, config)[0] == ["HR Business Partner"]
    assert matched_roles(hr_other, config)[0] == []
    jobs = [construction, it, unknown, hr_admin, hr_other]
    assert construction not in select_for_ranking(jobs, config)


def test_occupation_filters_dont_change_fingerprint():
    plain = RankingConfig(target_roles=[TargetRole(name="Project manager")])
    filtered = RankingConfig(
        target_roles=[TargetRole(name="Project manager", exclude_occupations=["Bygg"])]
    )
    assert plain.fingerprint() == filtered.fingerprint()


class ConcurrentFakeLLM(FakeLLM):
    """Only answers once `parallel` calls are in flight at the same time."""

    def __init__(self, parallel):
        super().__init__()
        self.barrier = threading.Barrier(parallel, timeout=5)
        self.threads = set()

    def complete(self, **kwargs):
        self.threads.add(threading.get_ident())
        self.barrier.wait()
        return super().complete(**kwargs)


def test_parallel_ranking_runs_calls_concurrently():
    jobs = [_job(i, f"Projektledare {i}") for i in range(1, 7)]
    llm = ConcurrentFakeLLM(parallel=3)
    # Store(":memory:") is bound to this thread, so any store access from a worker
    # thread would raise: budget checks and writes must stay on the calling thread.
    store, budgeted, _, config = _setup(jobs, llm)
    report = run_ranking(store, budgeted, config, CV, max_parallel=3)
    assert (report.ranked, report.failed) == (6, 0)
    assert len(llm.threads) == 3 and threading.get_ident() not in llm.threads
    assert len(ranked_jobs(store, config)) == 6
    assert store.llm_cost_since(datetime(2000, 1, 1, tzinfo=UTC)) > 0  # usage recorded


def test_parallel_ranking_respects_cap_and_failures():
    jobs = [_job(i, f"Projektledare {i}", published_day=i) for i in range(1, 8)]
    config = load_ranking_config(EXAMPLE)
    config.prefilter.max_llm_calls_per_run = 5
    llm = FakeLLM(fail_titles={"Projektledare 7"})  # newest, so ranked first
    store, budgeted, _, _ = _setup(jobs, llm, config=config)
    report = run_ranking(store, budgeted, config, CV, max_parallel=4)
    assert (report.ranked, report.failed, report.deferred) == (4, 1, 2)
    assert len(llm.prompts) == 5


def test_score_breakdown_matches_final_score():
    config = load_ranking_config(EXAMPLE)
    for a in [
        _assessment(),
        _assessment(language="en", swedish="merit"),
        _assessment(fit=10, success=0, swedish="required"),
        _assessment(fit=100, success=100, language="en"),
    ]:
        breakdown = score_breakdown(a, config)
        assert breakdown.total == final_score(a, config)
    breakdown = score_breakdown(_assessment(language="en", swedish="merit"), config)
    assert breakdown.combined == 72
    assert breakdown.adjustments == [("Ad written in English", 10), ("Swedish a merit", -5)]


def test_occupation_verdict_reasons():
    role = TargetRole(name="PM", exclude_occupations=["Bygg"], except_occupations=["logistik"])
    assert role.occupation_verdict(None, None) == (True, None)
    assert role.occupation_verdict("Bygg och anläggning", "Snickare") == (
        False,
        "occupation excluded (Bygg)",
    )
    assert role.occupation_verdict("Bygg", "logistik") == (True, None)
    hr = TargetRole(name="HR", include_occupations=["Administration"])
    assert hr.occupation_verdict("Data/IT", None) == (
        False,
        "occupation not in include_occupations",
    )


def test_prefilter_status_reports_roles_and_exclusions():
    pm = TargetRole(name="Project manager", aliases=["projektledare"], exclude_occupations=["Bygg"])
    hr = TargetRole(name="HR Business Partner", aliases=["HRBP"])
    config = RankingConfig(target_roles=[pm, hr])
    job = _with_occupation(
        _job(1, "Projektledare", "Du jobbar nära vår HRBP."), "Bygg och anläggning", None
    )
    status = prefilter_status(job, config)
    assert status.passed
    assert status.roles == ["HR Business Partner"] and not status.in_title
    assert status.roles_unfiltered == ["Project manager", "HR Business Partner"]
    assert status.body_only == ["HR Business Partner"]
    assert status.excluded == {"Project manager": "occupation excluded (Bygg)"}
    none = prefilter_status(_job(2, "Lagerarbetare"), config)
    assert not none.passed and none.roles_unfiltered == []


def test_filter_fingerprint_includes_occupation_filters():
    plain = RankingConfig(target_roles=[TargetRole(name="Project manager")])
    filtered = RankingConfig(
        target_roles=[TargetRole(name="Project manager", exclude_occupations=["Bygg"])]
    )
    assert plain.filter_fingerprint() != filtered.filter_fingerprint()


def test_zero_cap_means_no_limit():
    jobs = [_job(i, f"Projektledare {i}", published_day=i) for i in range(1, 9)]
    config = load_ranking_config(EXAMPLE)
    config.prefilter.max_llm_calls_per_run = 0
    store, budgeted, llm, _ = _setup(jobs, config=config)
    report = run_ranking(store, budgeted, config, CV, max_parallel=3)
    assert (report.ranked, report.deferred, report.stopped_reason) == (8, 0, None)


def test_a_streak_of_failures_stops_the_run():
    """A provider that is rate-limiting shouldn't be asked for every remaining job."""
    jobs = [_job(i, f"Projektledare {i}", published_day=i) for i in range(1, 11)]
    config = load_ranking_config(EXAMPLE)
    config.prefilter.max_llm_calls_per_run = 0
    config.prefilter.stop_after_failures = 3
    llm = FakeLLM(fail_titles={f"Projektledare {i}" for i in range(3, 11)})  # all but the 2 oldest
    store, budgeted, _, _ = _setup(jobs, llm, config=config)
    report = run_ranking(store, budgeted, config, CV)  # one at a time: a clean order
    assert (report.ranked, report.failed, report.deferred) == (0, 3, 7)
    assert "3 failures in a row" in report.stopped_reason
    assert len(llm.prompts) == 3  # nothing more was sent

    # A success resets the streak, so scattered failures don't stop the run.
    llm = FakeLLM(fail_titles={"Projektledare 2", "Projektledare 4", "Projektledare 6"})
    store, budgeted, _, _ = _setup(jobs, llm, config=config)
    report = run_ranking(store, budgeted, config, CV)
    assert (report.ranked, report.failed, report.stopped_reason) == (7, 3, None)

    config.prefilter.stop_after_failures = 0  # never stop early
    llm = FakeLLM(fail_titles={f"Projektledare {i}" for i in range(1, 11)})
    store, budgeted, _, _ = _setup(jobs, llm, config=config)
    assert run_ranking(store, budgeted, config, CV).failed == 10
