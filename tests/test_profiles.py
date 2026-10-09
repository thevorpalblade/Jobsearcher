"""Several candidates (profiles) sharing one job pool (docs/m10-multi-user.md)."""

import sqlite3
from pathlib import Path

import pytest
import yaml
from conftest import make_assessment, make_job
from fastapi.testclient import TestClient
from test_pipeline import FakeSource
from test_ranking import FakeLLM
from test_web import log_in

from jobsearcher import cli
from jobsearcher.auth import USER
from jobsearcher.config import DEFAULT_PROFILE, Config, LLMConfig, SearchConfig, load_config
from jobsearcher.llm import BudgetedLLM, BudgetTracker
from jobsearcher.models import ApplicationState
from jobsearcher.pipeline import all_profiles, matches_filters, profiles_keywords, run_search
from jobsearcher.ranking import load_ranking_config, run_ranking
from jobsearcher.ranking.ranker import Ranking
from jobsearcher.store import Store
from jobsearcher.web import create_app

RANKING_YAML = """\
target_roles:
  - name: {role}
preferences: {{}}
prefilter: {{require_role_mention: true}}
"""


def make_profile(root: Path, slug: str, role: str, locations: list[str], name: str = "") -> None:
    folder = root / "profiles" / slug
    (folder / "cvs").mkdir(parents=True)
    (folder / "cvs" / "master.md").write_text(f"# {slug}\n{role} for ten years.")
    (folder / "ranking.yaml").write_text(RANKING_YAML.format(role=role))
    settings = {"name": name, "search": {"locations": locations}}
    (folder / "profile.yaml").write_text(yaml.safe_dump(settings))


@pytest.fixture
def two(tmp_path):
    """A setup with two candidates: Anna in Stockholm, Bo in Göteborg."""
    (tmp_path / "config.yaml").write_text("search: {expire_after_days: 5}\n")
    make_profile(tmp_path, "anna", "Projektledare", ["Stockholm"], name="Anna")
    make_profile(tmp_path, "bo", "Controller", ["Göteborg"])
    return load_config(tmp_path / "config.yaml")


def test_without_a_profiles_folder_there_is_one_default_profile(tmp_path):
    config = load_config(tmp_path / "config.yaml")
    assert config.profile_slugs() == [DEFAULT_PROFILE]
    assert config.for_profile().cv_path == config.cv_path
    assert config.for_profile().profile_file is None
    assert Config().profile_slugs() == [DEFAULT_PROFILE]  # no load_config: no folder


def test_for_profile_points_at_the_profiles_files(two, tmp_path):
    assert two.profile_slugs() == ["anna", "bo"]
    anna = two.for_profile()  # the first
    assert anna.profile == "anna"
    assert anna.cv_path == tmp_path / "profiles" / "anna" / "cvs" / "master.md"
    assert anna.ranking_config == tmp_path / "profiles" / "anna" / "ranking.yaml"
    assert anna.search.locations == ["Stockholm"]
    assert anna.search.expire_after_days == 5  # global, from config.yaml
    assert anna.web.user_name == "Anna"
    assert two.for_profile("bo").search.locations == ["Göteborg"]
    assert anna.draft_instructions == ""
    path = tmp_path / "profiles" / "bo" / "profile.yaml"
    path.write_text(path.read_text() + "draft_instructions: Use my Swedish number.\n")
    assert two.for_profile("bo").draft_instructions == "Use my Swedish number."
    with pytest.raises(ValueError, match="No profile 'cecilia'"):
        two.for_profile("cecilia")


def test_store_keeps_each_profiles_rows_apart(tmp_path):
    store = Store(tmp_path / "db")
    anna = Store(tmp_path / "db", profile="anna")
    job = make_job(1, "Projektledare")
    store.upsert_job(job)
    assert anna.get_job(job.id) == job  # jobs are shared
    ranking = Ranking(job_id=job.id, input_hash="h", model="m", assessment=make_assessment())
    anna.save_ranking(job.id, "h", ranking.model_dump_json())
    anna.set_application(job.id, ApplicationState.SHORTLISTED, "")
    anna.save_draft(job.id, "d", "{}")
    assert list(anna.latest_rankings()) == [job.id] and anna.get_ranking(job.id, "h")
    assert store.latest_rankings() == {} and store.get_ranking(job.id, "h") is None
    assert store.applications() == {} and store.tracked_job_records() == []
    assert store.latest_drafts() == {} and len(anna.latest_drafts()) == 1
    assert [r.job.id for r in anna.tracked_job_records()] == [job.id]


def test_an_old_database_gets_profiles_without_losing_rows(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE rankings (job_id TEXT NOT NULL, input_hash TEXT NOT NULL,
            data TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (job_id, input_hash));
        CREATE TABLE applications (job_id TEXT PRIMARY KEY, state TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
        CREATE TABLE llm_usage (ts TEXT NOT NULL, model TEXT NOT NULL, purpose TEXT NOT NULL,
            input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL);
        INSERT INTO rankings VALUES ('j1', 'h', '{}', '2026-10-01');
        INSERT INTO applications VALUES ('j1', 'applied', 'sent', '2026-10-01');
        INSERT INTO llm_usage VALUES ('2026-10-01', 'm', 'ranking', 1, 2, 0.5);
        """
    )
    conn.close()
    store = Store(db)
    assert store.get_ranking("j1", "h") == "{}"
    assert store.applications()["j1"].notes == "sent"
    usage = store.conn.execute("SELECT profile, cost_usd FROM llm_usage").fetchall()
    assert [tuple(r) for r in usage] == [(DEFAULT_PROFILE, 0.5)]
    store.rename_profile(DEFAULT_PROFILE, "anna")
    assert Store(db).applications() == {}  # opening again doesn't migrate twice
    assert Store(db, profile="anna").applications()["j1"].state == "applied"


def test_one_search_serves_every_profile(two):
    stockholm = make_job(1, "Projektledare", location="Stockholm")
    goteborg = make_job(2, "Controller", location="Göteborg")
    malmo = make_job(3, "Projektledare", location="Malmö")
    store = Store(":memory:")
    profiles = all_profiles(two)
    assert profiles_keywords(profiles) == ["Projektledare", "Controller"]
    source = FakeSource("platsbanken", [stockholm, goteborg, malmo])
    report = run_search(two, store, [source], profiles=profiles)
    # Kept if it's in anyone's region; Malmö is no one's.
    assert {j.id for j in store.iter_jobs()} == {stockholm.id, goteborg.id}
    assert (report.fetched, report.filtered_out) == (3, 1)


def test_ranking_picks_each_profiles_own_jobs_from_the_shared_pool(two):
    store = Store(two.db_path)
    jobs = [
        make_job(1, "Projektledare", location="Stockholm"),
        make_job(2, "Controller", location="Göteborg"),
        make_job(3, "Projektledare", location="Göteborg"),  # Bo's region, Anna's role
    ]
    for job in jobs:
        store.upsert_job(job)
    for slug, expected in (("anna", ["Projektledare"]), ("bo", ["Controller"])):
        profile = two.for_profile(slug)
        scoped = Store(two.db_path, profile=slug)
        llm = FakeLLM()
        budgeted = BudgetedLLM(llm, BudgetTracker(scoped, LLMConfig()), "ranking")
        report = run_ranking(
            scoped,
            budgeted,
            load_ranking_config(profile.ranking_config),
            "cv",
            wanted=lambda job, p=profile: matches_filters(job, p.search),
        )
        titles = [prompt.splitlines()[0].removeprefix("Job title: ") for _, prompt in llm.prompts]
        assert titles == expected and report.ranked == 1
        assert len(scoped.latest_rankings()) == 1


def test_migrate_profiles_moves_files_rows_and_drafts(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text(
        "search: {locations: [Stockholm], keywords: [HR]}\nweb: {user_name: Anna}\n"
    )
    (tmp_path / "ranking.yaml").write_text(RANKING_YAML.format(role="HR"))
    (tmp_path / "companies.yaml").write_text("companies: []\n")
    (tmp_path / "cvs").mkdir()
    (tmp_path / "cvs" / "master.md").write_text("# Anna")
    (tmp_path / "data" / "drafts" / "job1" / "v1").mkdir(parents=True)
    store = Store(tmp_path / "data" / "jobsearcher.db")
    store.upsert_job(make_job(1, "HR"))
    store.set_application(make_job(1, "HR").id, "applied", "")
    store.close()

    config_arg = ["--config", str(tmp_path / "config.yaml")]
    assert cli.main([*config_arg, "migrate-profiles", "Anna"]) == 2  # not a plain name
    assert cli.main([*config_arg, "migrate-profiles", "anna"]) == 0
    folder = tmp_path / "profiles" / "anna"
    assert (folder / "cvs" / "master.md").read_text() == "# Anna"
    assert (folder / "ranking.yaml").is_file() and (folder / "companies.yaml").is_file()
    assert not (tmp_path / "ranking.yaml").exists() and not (tmp_path / "cvs").exists()
    assert (tmp_path / "data" / "drafts" / "anna" / "job1" / "v1").is_dir()

    config = load_config(tmp_path / "config.yaml").for_profile()
    assert config.profile == "anna" and config.web.user_name == "Anna"
    assert not config.own_keys  # the existing setup keeps using .env's keys
    assert "web: {user_name: Anna}" in (tmp_path / "config.yaml").read_text()  # kept
    assert config.search.locations == ["Stockholm"] and config.search.keywords == ["HR"]
    assert len(Store(config.db_path, profile="anna").applications()) == 1
    assert Store(config.db_path).applications() == {}
    assert cli.main([*config_arg, "migrate-profiles", "bo"]) == 2  # already done
    assert cli.main([*config_arg, "--profile", "nobody", "list"]) == 2


def test_web_shows_one_profile_and_its_settings_file(two, tmp_path):
    job = make_job(1, "Projektledare", location="Stockholm")
    store = Store(two.db_path)
    store.upsert_job(job)
    bo = Store(two.db_path, profile="bo")
    ranking = Ranking(job_id=job.id, input_hash="h", model="m", assessment=make_assessment())
    bo.save_ranking(job.id, "h", ranking.model_dump_json())  # Bo's, not Anna's
    with TestClient(create_app(two, tmp_path / "config.yaml")) as client:
        log_in(client, two.db_path, "anna", USER, "anna")
        assert client.app.state.web.state_for("anna").config.profile == "anna"
        assert "Welcome, Anna" in client.get("/").text
        settings = client.get("/settings").text
        assert "profile.yaml" in settings
        assert "Stockholm" in client.get("/settings/files/profile").text
        assert "Not ranked yet" in client.get(f"/jobs/{job.id}").text  # Bo's isn't shown


def test_matches_filters_is_per_profile():
    anna = SearchConfig(locations=["Stockholm"])
    bo = SearchConfig(locations=["Göteborg"], exclude_keywords=["konsult"])
    job = make_job(1, "Konsult", location="Göteborg")
    assert not matches_filters(job, anna) and not matches_filters(job, bo)


def test_a_profile_not_searching_yet_doesnt_widen_the_search(two, tmp_path):
    """A new, empty profile has no region: it mustn't make the search keep all of Sweden."""
    (tmp_path / "config.yaml").write_text("search: {expire_after_days: 5}\n")
    assert cli.main(["--config", str(tmp_path / "config.yaml"), "profiles", "add", "cecilia"]) == 0
    config = load_config(tmp_path / "config.yaml")
    assert config.profile_slugs() == ["anna", "bo", "cecilia"]
    cecilia = config.for_profile("cecilia")
    assert cecilia.web.user_name == "Cecilia" and cecilia.own_keys
    assert load_ranking_config(cecilia.ranking_config).target_roles == []
    store = Store(":memory:")
    malmo = make_job(3, "Projektledare", location="Malmö")
    run_search(config, store, [FakeSource("platsbanken", [malmo])], profiles=all_profiles(config))
    assert list(store.iter_jobs()) == []  # still only anna's and bo's regions
    args = ["--config", str(tmp_path / "config.yaml"), "profiles", "add"]
    assert cli.main([*args, "cecilia"]) == 1  # exists
    assert cli.main([*args, "Not Plain"]) == 2
