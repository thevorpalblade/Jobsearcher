import json
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_assessment, make_job
from fastapi.testclient import TestClient

from jobsearcher import cli
from jobsearcher.config import Config
from jobsearcher.models import ApplicationState, Contact, Job, SourceRef
from jobsearcher.ranking import load_ranking_config
from jobsearcher.ranking.config import RankingConfig, TargetRole
from jobsearcher.ranking.prefilter import matched_roles
from jobsearcher.ranking.ranker import JobAssessment, Ranking, input_hash
from jobsearcher.store import JobRecord, Store
from jobsearcher.web import create_app
from jobsearcher.web.views import PrefilterMemo

ROOT = Path(__file__).parent.parent
CV = "# Anna Andersson\nHR Business Partner at Exempel AB, 2019-2026."
RANKING_YAML = """\
target_roles:
  - name: Project manager
    aliases: [projektledare]
    exclude_occupations: [Bygg]
  - name: HR Business Partner
    aliases: [HRBP]
weights: {fit: 0.6, success: 0.4}
adjustments: {english_ad: 10, swedish_required: -10, swedish_merit: -5}
drafting: {min_score: 70}
"""


@dataclass
class Web:
    client: TestClient
    store: Store
    config: Config

    def add(self, job: Job, assessment: JobAssessment | None = None, seen=None, **ranking) -> Job:
        """Store a job (first seen at `seen`) and, optionally, a current ranking for it."""
        self.store.upsert_job(job, now=seen)
        if assessment is not None:
            rconfig = load_ranking_config(self.config.ranking_config)
            model = ranking.pop("model", self.config.llm.ranking.model)
            h = ranking.pop("input_hash", None) or input_hash(job, CV, rconfig, model)
            data = Ranking(job_id=job.id, input_hash=h, model=model, assessment=assessment)
            self.store.save_ranking(job.id, h, data.model_dump_json())
        return job


@pytest.fixture
def web(tmp_path):
    (tmp_path / "ranking.yaml").write_text(RANKING_YAML)
    (tmp_path / "master.md").write_text(CV)
    # A file DB: every request opens its own connection, which :memory: can't share.
    config = Config(
        data_dir=tmp_path / "data",
        ranking_config=tmp_path / "ranking.yaml",
        cv_path=tmp_path / "master.md",
    )
    with TestClient(create_app(config)) as client:
        store = Store(config.db_path)
        yield Web(client, store, config)
        store.close()


def test_empty_database_list_and_health(web):
    response = web.client.get("/jobs")
    assert response.status_code == 200
    assert "No jobs" in response.text
    assert web.client.get("/healthz").text == "ok"


def test_fresh_install_creates_the_database(tmp_path):
    config = Config(data_dir=tmp_path / "new", ranking_config=tmp_path / "missing.yaml")
    with TestClient(create_app(config)) as client:
        assert client.get("/jobs").status_code == 200
    assert config.db_path.exists()


def test_static_files_are_served(web):
    response = web.client.get("/static/htmx.min.js")
    assert response.status_code == 200
    assert "htmx 2.0." in response.text.splitlines()[0]  # vendored, version in the header
    assert web.client.get("/static/style.css").status_code == 200


def test_no_api_docs(web):
    assert web.client.get("/docs").status_code == 404
    assert web.client.get("/openapi.json").status_code == 404


def test_cmd_web_runs_uvicorn_with_configured_host_and_port(monkeypatch, tmp_path):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    config = Config(data_dir=tmp_path)
    config.web.host, config.web.port = "0.0.0.0", 9123
    args = cli.argparse.Namespace(host=None, port=None, reload=False, config=None)
    assert cli.cmd_web(config, args) == 0
    [(app, kwargs)] = calls
    assert kwargs == {"host": "0.0.0.0", "port": 9123, "workers": 1}
    args.port = 8099
    cli.cmd_web(config, args)
    assert calls[-1][1]["port"] == 8099


def test_templates_and_static_files_are_packaged(tmp_path):
    from hatchling.builders.wheel import WheelBuilder

    [wheel] = WheelBuilder(str(ROOT)).build(directory=str(tmp_path), versions=["standard"])
    names = set(zipfile.ZipFile(wheel).namelist())
    assert "jobsearcher/web/static/htmx.min.js" in names
    assert "jobsearcher/web/static/style.css" in names
    assert "jobsearcher/web/templates/base.html" in names


def _titles(html):
    """Job titles in the order the table lists them."""
    return re.findall(r'<a href="/jobs/\w+">([^<]+)</a>', html)


def _row_html(html):
    """{title: that table row's HTML}."""
    rows = {}
    for tr in html.split("<tr")[2:]:  # skip the header row
        [title] = _titles(tr)
        rows[title] = tr
    return rows


def _touch_newer(path):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_list_orders_ranked_jobs_by_final_score(web):
    web.add(make_job(1, "Projektledare A"), make_assessment(60, 60))  # 60
    web.add(make_job(2, "Projektledare B"), make_assessment(70, 50, language="en"))  # 62 + 10
    web.add(make_job(3, "Projektledare C"), make_assessment(90, 90, swedish="required"))  # 80
    web.add(make_job(4, "Projektledare D"))  # not ranked yet: not in the default view
    html = web.client.get("/jobs").text
    assert _titles(html) == ["Projektledare C", "Projektledare B", "Projektledare A"]
    assert '<span class="score top">80</span>' in html  # at or above drafting.min_score
    assert '<span class="score">60</span>' in html


def test_editing_ranking_yaml_reorders_without_reranking(web):
    web.add(make_job(1, "Projektledare Fit"), make_assessment(90, 10))
    web.add(make_job(2, "Projektledare Success"), make_assessment(10, 90))
    assert _titles(web.client.get("/jobs").text)[0] == "Projektledare Fit"
    path = web.config.ranking_config
    path.write_text(path.read_text().replace("{fit: 0.6, success: 0.4}", "{fit: 0, success: 1}"))
    _touch_newer(path)
    assert _titles(web.client.get("/jobs").text)[0] == "Projektledare Success"


def test_stale_ranking_is_flagged(web):
    web.add(make_job(1, "Projektledare Current"), make_assessment())
    web.add(make_job(2, "Projektledare Old"), make_assessment(), input_hash="from-an-old-cv")
    rows = _row_html(web.client.get("/jobs").text)
    assert "stale" not in rows["Projektledare Current"]
    assert "stale" in rows["Projektledare Old"]


def test_a_new_reference_cv_makes_rankings_stale(web):
    web.add(make_job(1, "Projektledare Current"), make_assessment())
    assert "stale" not in web.client.get("/jobs").text
    (web.config.cv_path.parent / "older.md").write_text("# Anna Andersson\nPayroll, 2015.")
    assert "stale" in _row_html(web.client.get("/jobs").text)["Projektledare Current"]


def test_prefilter_memo_reuses_results_until_the_filters_change():
    config = RankingConfig(target_roles=[TargetRole(name="Project manager")])
    record = JobRecord(make_job(1, "Project manager"), datetime.now(UTC), datetime.now(UTC))
    memo = PrefilterMemo()
    first = memo.get(record, config, config.filter_fingerprint())
    assert memo.get(record, config, config.filter_fingerprint()) is first
    config.target_roles[0].exclude_occupations = ["Bygg"]
    assert memo.get(record, config, config.filter_fingerprint()) is not first


@pytest.fixture
def filter_jobs(web):
    now = datetime.now(UTC)
    web.add(
        make_job(
            1,
            "Projektledare Remote",
            location="Göteborg",
            remote=True,
            deadline=now + timedelta(days=5),
            contacts=[Contact(name="Per", provenance="llm:ad_text")],
            sources=[SourceRef(source="jobtech_links", source_id="1")],
        ),
        make_assessment(90, 90, language="en", matched_role="Project manager"),
    )
    web.add(
        make_job(2, "HRBP Stockholm", deadline=now + timedelta(days=30)),
        make_assessment(70, 70, swedish="required"),
    )
    web.add(
        make_job(3, "Projektledare Merit", deadline=now - timedelta(days=1)),
        make_assessment(60, 60, swedish="merit", matched_role="Project manager"),
        seen=now - timedelta(days=10),
    )
    return web


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", ["Projektledare Remote", "HRBP Stockholm", "Projektledare Merit"]),
        ("min_score=60", ["Projektledare Remote", "HRBP Stockholm"]),
        ("source=jobtech_links", ["Projektledare Remote"]),
        ("location=stockholm", ["HRBP Stockholm", "Projektledare Merit"]),
        ("remote=true", ["Projektledare Remote"]),
        ("remote=false", ["HRBP Stockholm", "Projektledare Merit"]),
        ("deadline_within=10", ["Projektledare Remote"]),  # past deadlines are hidden too
        ("role=project+manager", ["Projektledare Remote", "Projektledare Merit"]),
        ("language=sv", ["HRBP Stockholm", "Projektledare Merit"]),
        ("swedish=exclude_required", ["Projektledare Remote", "Projektledare Merit"]),
        ("swedish=not_mentioned", ["Projektledare Remote"]),
        ("has_contact=true", ["Projektledare Remote"]),
        ("new=3", ["Projektledare Remote", "HRBP Stockholm"]),
        ("q=hrbp", ["HRBP Stockholm"]),
        ("q=company+3", ["Projektledare Merit"]),
        ("sort=deadline", ["Projektledare Merit", "Projektledare Remote", "HRBP Stockholm"]),
        ("sort=fit&swedish=exclude_required", ["Projektledare Remote", "Projektledare Merit"]),
        (
            "sort=deadline&dir=desc",
            ["HRBP Stockholm", "Projektledare Remote", "Projektledare Merit"],
        ),
        ("sort=score&dir=asc", ["Projektledare Merit", "HRBP Stockholm", "Projektledare Remote"]),
        ("sort=title", ["HRBP Stockholm", "Projektledare Merit", "Projektledare Remote"]),
        ("sort=title&dir=desc", ["Projektledare Remote", "Projektledare Merit", "HRBP Stockholm"]),
        ("sort=location", ["Projektledare Remote", "HRBP Stockholm", "Projektledare Merit"]),
        ("sort=remote", ["Projektledare Remote", "HRBP Stockholm", "Projektledare Merit"]),
        ("sort=contact", ["Projektledare Remote", "HRBP Stockholm", "Projektledare Merit"]),
        ("sort=language", ["Projektledare Remote", "Projektledare Merit", "HRBP Stockholm"]),
    ],
)
def test_list_filters(filter_jobs, query, expected):
    response = filter_jobs.client.get(f"/jobs?{query}")
    assert response.status_code == 200
    assert _titles(response.text) == expected


def test_empty_filter_fields_are_ignored(filter_jobs):
    # What a submitted form sends without JavaScript.
    query = (
        "view=ranked&q=&min_score=&role=&source=&location=&remote=&deadline_within="
        "&language=&swedish=any&new=&sort=score"
    )
    response = filter_jobs.client.get(f"/jobs?{query}")
    assert response.status_code == 200
    assert len(_titles(response.text)) == 3
    assert filter_jobs.client.get("/jobs?min_score=abc").status_code == 422


def test_htmx_requests_get_only_the_rows(filter_jobs):
    full = filter_jobs.client.get("/jobs?q=hrbp")
    assert "<html" in full.text and 'id="rows"' in full.text
    fragment = filter_jobs.client.get("/jobs?q=hrbp", headers={"HX-Request": "true"})
    assert "<html" not in fragment.text and "<table" in fragment.text
    assert _titles(fragment.text) == ["HRBP Stockholm"]
    assert fragment.headers["Vary"] == "HX-Request"
    restore = filter_jobs.client.get(
        "/jobs?q=hrbp", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}
    )
    assert "<html" in restore.text


def test_new_badge(filter_jobs):
    rows = _row_html(filter_jobs.client.get("/jobs").text)
    assert "chip new" in rows["HRBP Stockholm"]
    assert "chip new" not in rows["Projektledare Merit"]


def _detail_job(**overrides):
    fields = {
        "company_org_nr": "556000-0000",
        "apply_url": "https://example.com/apply?id=1",
        "apply_email": "jobb@example.com",
        "contacts": [
            Contact(
                name="Anna Chef",
                role="HR-chef",
                email="anna@example.com",
                provenance="platsbanken:application_contacts",
            ),
            Contact(name="Per Persson", phone="070-123 45 67", provenance="llm:ad_text"),
            Contact(email="info@example.com", provenance="platsbanken:ad_text"),
        ],
        "sources": [
            SourceRef(source="platsbanken", source_id="1", url="https://arbetsformedlingen.se/1")
        ],
    } | overrides
    text = fields.pop("description", "Vi söker en projektledare.")
    return make_job(1, "Projektledare", text, **fields)


def test_job_page_shows_assessment_score_and_contacts(web):
    job = web.add(
        _detail_job(),
        make_assessment(
            80,
            60,
            language="en",
            swedish="merit",
            rationale="Strong change management background.",
            red_flags=["Travel 50%"],
            missing_requirements=["PMP certification"],
        ),
    )
    response = web.client.get(f"/jobs/{job.id}")
    assert response.status_code == 200
    html = response.text
    assert "Strong change management background." in html
    assert "Travel 50%" in html and "PMP certification" in html
    assert "→ <strong>72</strong>" in html  # 80 × 0.6 + 60 × 0.4
    assert "Ad written in English: +10" in html and "Swedish a merit: -5" in html
    assert "Final score: <strong>77</strong>" in html
    assert 'href="https://example.com/apply?id=1"' in html
    assert 'href="mailto:jobb@example.com"' in html
    assert 'href="https://arbetsformedlingen.se/1"' in html
    assert 'rel="noopener noreferrer"' in html
    assert "Platsbanken (structured)" in html
    assert "Named in the ad (extracted by the LLM, check before use)" in html
    assert "(llm:ad_text)" in html  # the raw provenance next to the label
    assert 'href="tel:0701234567"' in html
    assert "generic mailbox" in html.split("info@example.com")[2]
    assert "556000-0000" in html
    assert "passes" in html and "stale" not in html


def test_unranked_and_excluded_job_pages(web):
    pending = web.add(make_job(1, "Projektledare IT"))
    excluded = web.add(make_job(2, "Projektledare bygg", occupation_field="Bygg och anläggning"))
    assert "will be ranked on the next run" in web.client.get(f"/jobs/{pending.id}").text
    html = web.client.get(f"/jobs/{excluded.id}").text
    assert "the prefilter excludes it" in html
    assert "Project manager: occupation excluded (Bygg)" in html


def test_old_ranking_format_says_it_will_be_re_ranked(web):
    job = web.add(make_job(1, "Projektledare"))
    web.store.save_ranking(job.id, "old", '{"job_id": "x", "score": 7}')
    assert "will be re-ranked" in web.client.get(f"/jobs/{job.id}").text
    rows = _row_html(web.client.get("/jobs?view=all").text)
    assert "will be re-ranked" in rows["Projektledare"]


def test_unknown_job_is_404(web):
    response = web.client.get("/jobs/does-not-exist")
    assert response.status_code == 404
    assert "Not found" in response.text
    assert web.client.get("/jobs/does-not-exist.json").status_code == 404


def test_job_json_matches_cli_show(web, capsys):
    job = web.add(_detail_job(), make_assessment())
    data = web.client.get(f"/jobs/{job.id}.json").json()
    cli.cmd_show(web.config, cli.argparse.Namespace(job_id=job.id))
    assert data == json.loads(capsys.readouterr().out)
    assert data["ranking"]["score"] == 72


def test_untrusted_ad_content_is_escaped(web):
    job = web.add(
        _detail_job(
            description='<script>alert("x")</script> <img src=x onerror=alert(1)>',
            url="javascript:alert(1)",
            apply_url="javascript:alert(2)",
        ),
        make_assessment(rationale="<b>bold</b>"),
    )
    html = web.client.get(f"/jobs/{job.id}").text
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "<img src=x" not in html
    assert "<b>bold</b>" not in html
    assert 'href="javascript:' not in html
    assert "javascript:alert(2)" in html  # shown as text instead


@pytest.fixture
def prefilter_jobs(web):
    web.add(make_job(1, "Projektledare IT", occupation_field="Data/IT", occupation_group="Dev"))
    web.add(make_job(2, "Konsult", "Du blir projektledare.", occupation_field="Data/IT"))
    web.add(
        make_job(
            3,
            "Projektledare bygg",
            occupation_field="Bygg och anläggning",
            occupation_group="Ingenjörer bygg",
        )
    )
    web.add(make_job(4, "Lagerarbetare"))
    web.add(make_job(5, "HRBP"), make_assessment())
    return web


def test_pending_and_excluded_views_with_reasons(prefilter_jobs):
    client = prefilter_jobs.client
    pending = _row_html(client.get("/jobs?view=pending").text)
    assert set(pending) == {"Projektledare IT", "Konsult"}
    assert "text only" in pending["Konsult"] and "title" in pending["Projektledare IT"]
    excluded = _row_html(client.get("/jobs?view=excluded").text)
    assert set(excluded) == {"Projektledare bygg", "Lagerarbetare"}
    assert "Project manager: occupation excluded (Bygg)" in excluded["Projektledare bygg"]
    assert "mentions no target role" in excluded["Lagerarbetare"]
    assert set(_titles(client.get("/jobs?view=all").text)) == {
        "Projektledare IT",
        "Konsult",
        "Projektledare bygg",
        "Lagerarbetare",
        "HRBP",
    }
    # The links on /prefilter narrow a view by role and occupation.
    query = "/jobs?view=excluded&role=Project+manager&occupation=Ingenj%C3%B6rer+bygg"
    assert _titles(client.get(query).text) == ["Projektledare bygg"]


def test_prefilter_page_counts_match_matched_roles(prefilter_jobs):
    config = load_ranking_config(prefilter_jobs.config.ranking_config)
    jobs = list(prefilter_jobs.store.iter_jobs())
    mentioned = sum(
        "Project manager" in matched_roles(j, config, apply_occupation_filters=False)[0]
        for j in jobs
    )
    kept = sum("Project manager" in matched_roles(j, config)[0] for j in jobs)
    html = prefilter_jobs.client.get("/prefilter").text
    assert (mentioned, kept) == (3, 2)
    assert f"Mentioned by {mentioned} open jobs: {kept} kept, 1 excluded" in html
    assert "5 open jobs" in html and "1 ranked" in html and "2 waiting for ranking" in html
    assert (
        '<a href="/jobs?view=excluded&amp;role=Project%20manager&amp;'
        'occupation=Bygg%20och%20anl%C3%A4ggning">1</a>'
    ) in html
    assert 'Bygg och anläggning</strong> <span class="chip bad">excluded</span>' in html


def test_expired_jobs_are_hidden_unless_asked_for(web):
    old = web.add(make_job(1, "Projektledare gammal"), make_assessment())
    web.add(make_job(2, "Projektledare ny"), make_assessment())
    web.store.conn.execute("UPDATE jobs SET last_seen = '2000-01-01' WHERE id = ?", (old.id,))
    web.store.conn.commit()
    web.store.expire_jobs(expire_after_days=3)
    assert _titles(web.client.get("/jobs").text) == ["Projektledare ny"]
    assert _titles(web.client.get("/jobs?view=expired").text) == ["Projektledare gammal"]
    assert "expired" in web.client.get(f"/jobs/{old.id}").text


def _usage(store, model, purpose, cost, tokens=(1000, 200)):
    store.record_llm_usage(datetime.now(UTC), model, purpose, *tokens, 0, 0, cost)


def test_budget_widget_on_every_page(web):
    job = web.add(make_job(1, "Projektledare"), make_assessment())
    _usage(web.store, "kimi-k2.6", "ranking", 1.23)
    _usage(web.store, "claude-code/claude-opus-5-5", "drafting", 0.0, (5000, 1000))
    for path in ("/", f"/jobs/{job.id}", "/prefilter", "/status", "/partials/budget"):
        html = web.client.get(path).text
        assert "LLM $1.23 of $20.00" in html, path
        assert "Claude Code: 1 calls, 6,000 tokens" in html, path
    assert 'hx-trigger="every 60s"' in web.client.get("/partials/budget").text
    # Drafting pauses at 80% of the budget: the marker sits there.
    assert 'class="marker" style="left: 80.0%"' in web.client.get("/jobs").text
    # HTMX list updates don't recompute it.
    assert "LLM $" not in web.client.get("/jobs", headers={"HX-Request": "true"}).text


def test_status_page(web):
    web.add(make_job(1, "Projektledare"), make_assessment())
    web.add(make_job(2, "Projektledare B"), make_assessment(), input_hash="stale")
    web.add(make_job(3, "Lagerarbetare"))
    web.store.set_last_run("platsbanken", datetime(2026, 9, 30, 4, 0, tzinfo=UTC))
    _usage(web.store, "kimi-k2.6", "ranking", 0.5)
    _usage(web.store, "kimi-k2.6", "ranking", 0.25)
    html = web.client.get("/status").text
    assert "2026-09-30 06:00" in html  # shown in Europe/Stockholm time
    assert '<a href="/jobs?view=ranked">2</a>' in html and "1 stale" in html
    assert '<a href="/jobs?view=excluded">1</a>' in html
    assert '<td>kimi-k2.6</td><td>ranking</td><td class="num">2</td>' in html
    assert "$0.75" in html


def test_llm_usage_summary(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    _usage(store, "a", "ranking", 1.0)
    _usage(store, "a", "ranking", 2.0)
    _usage(store, "b", "drafting", 0.5)
    store.record_llm_usage(datetime(2000, 1, 1, tzinfo=UTC), "a", "ranking", 1, 1, 0, 0, 9.0)
    summary = store.llm_usage_summary(datetime(2026, 1, 1, tzinfo=UTC))
    assert [(u.model, u.purpose, u.calls, u.cost_usd) for u in summary] == [
        ("a", "ranking", 2, 3.0),
        ("b", "drafting", 1, 0.5),
    ]
    assert summary[0].input_tokens == 2000


HX = {"HX-Request": "true"}


def test_store_application_tracking(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    job = make_job(1, "Projektledare")
    store.upsert_job(job)
    assert store.get_application(job.id) is None
    store.set_application(job.id, ApplicationState.APPLIED, "Sent 1 Oct")
    app = store.get_application(job.id)
    assert (app.state, app.notes) == (ApplicationState.APPLIED, "Sent 1 Oct")
    assert set(store.applications()) == {job.id}
    # Notes keep a "new" job tracked; without them it is untracked again.
    assert store.set_application(job.id, ApplicationState.NEW, "Ask Per").state == "new"
    assert store.set_application(job.id, ApplicationState.NEW, "  ") is None
    assert store.get_application(job.id) is None


def test_tracking_buttons_and_notes(web):
    job = web.add(make_job(1, "Projektledare"), make_assessment())
    page = web.client.get(f"/jobs/{job.id}").text
    assert 'hx-post="/jobs/' in page and 'value="applied"' in page

    response = web.client.post(f"/jobs/{job.id}/state", data={"state": "applied"}, headers=HX)
    assert response.status_code == 200
    assert "<html" not in response.text and 'id="tracking"' in response.text
    assert 'value="applied" class="on"' in response.text
    response = web.client.post(
        f"/jobs/{job.id}/notes", data={"notes": "Called <b>Per</b>"}, headers=HX
    )
    assert "Called &lt;b&gt;Per&lt;/b&gt;" in response.text
    app = web.store.get_application(job.id)
    assert (app.state, app.notes) == ("applied", "Called <b>Per</b>")
    assert "applied" in _row_html(web.client.get("/jobs").text)["Projektledare"]

    # Clearing the notes (an empty field) and going back to "new" untracks the job.
    assert web.client.post(f"/jobs/{job.id}/notes", data={"notes": ""}, headers=HX).is_success
    assert web.store.get_application(job.id).notes == ""
    web.client.post(f"/jobs/{job.id}/state", data={"state": "new"}, headers=HX)
    assert web.store.get_application(job.id) is None

    assert (
        web.client.post(f"/jobs/{job.id}/state", data={"state": "bogus"}, headers=HX).status_code
        == 422
    )
    assert (
        web.client.post("/jobs/nope/state", data={"state": "applied"}, headers=HX).status_code
        == 404
    )


def test_posts_without_hx_request_are_refused(web):
    job = web.add(make_job(1, "Projektledare"))
    for path, data in (("state", {"state": "applied"}), ("notes", {"notes": "x"})):
        response = web.client.post(f"/jobs/{job.id}/{path}", data=data)
        assert response.status_code == 403
    assert web.store.get_application(job.id) is None


def test_ignored_jobs_are_hidden_except_in_tracked_view(web):
    job = web.add(make_job(1, "Projektledare ignorerad"), make_assessment())
    web.add(make_job(2, "Projektledare"), make_assessment())
    web.client.post(f"/jobs/{job.id}/state", data={"state": "ignored"}, headers=HX)
    assert _titles(web.client.get("/jobs").text) == ["Projektledare"]
    assert _titles(web.client.get("/jobs?view=all").text) == ["Projektledare"]
    assert _titles(web.client.get("/jobs?view=tracked").text) == ["Projektledare ignorerad"]


def test_tracked_jobs_stay_visible_after_expiry(web):
    job = web.add(make_job(1, "Projektledare sökt"), make_assessment())
    web.client.post(f"/jobs/{job.id}/state", data={"state": "applied"}, headers=HX)
    web.store.conn.execute("UPDATE jobs SET last_seen = '2000-01-01'")
    web.store.conn.commit()
    assert web.store.expire_jobs(expire_after_days=3) == 1
    assert _titles(web.client.get("/jobs").text) == []
    rows = _row_html(web.client.get("/jobs?view=tracked").text)
    assert "applied" in rows["Projektledare sökt"] and "expired" in rows["Projektledare sökt"]


def test_column_headers_sort_and_flip(filter_jobs):
    page = filter_jobs.client.get("/jobs?min_score=10").text
    # Each header links to a sort that keeps the other filters; the sorted column
    # (score, by default best first) shows an arrow and flips when clicked.
    assert 'href="/jobs?view=ranked&amp;min_score=10&amp;swedish=any&amp;sort=title"' in page
    assert re.search(r'class="sort active" href="[^"]*sort=score&amp;dir=asc">Score ▼', page)
    flipped = filter_jobs.client.get("/jobs?min_score=10&sort=score&dir=asc").text
    assert re.search(r'class="sort active" href="[^"]*sort=score">Score ▲', flipped)
    # The filter form keeps the direction, so changing a filter keeps the sort.
    assert '<input type="hidden" name="dir" value="asc">' in flipped


def test_rows_without_a_value_sort_last_both_ways(filter_jobs):
    filter_jobs.add(make_job(4, "Projektledare Okänd"), make_assessment(50, 50))
    for direction in ("asc", "desc"):
        titles = _titles(filter_jobs.client.get(f"/jobs?sort=deadline&dir={direction}").text)
        assert titles[-1] == "Projektledare Okänd"  # no deadline


def test_static_urls_are_versioned(web):
    """Browsers cache CSS/JS; a version in the URL makes them fetch updated files."""
    page = web.client.get("/jobs").text
    css = re.search(r'href="(/static/style\.css\?v=\d+)"', page)
    assert css and re.search(r'src="/static/htmx\.min\.js\?v=\d+"', page)
    assert web.client.get(css.group(1)).status_code == 200
