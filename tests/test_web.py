import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobsearcher import cli
from jobsearcher.config import Config
from jobsearcher.models import Job
from jobsearcher.ranking import load_ranking_config
from jobsearcher.ranking.ranker import JobAssessment, Ranking, input_hash
from jobsearcher.store import Store
from jobsearcher.web import create_app

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

    def add(self, job: Job, assessment: JobAssessment | None = None, **ranking) -> Job:
        """Store a job and, optionally, a current ranking for it."""
        self.store.upsert_job(job)
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
    response = web.client.get("/")
    assert response.status_code == 200
    assert "No jobs" in response.text
    assert web.client.get("/healthz").text == "ok"


def test_fresh_install_creates_the_database(tmp_path):
    config = Config(data_dir=tmp_path / "new", ranking_config=tmp_path / "missing.yaml")
    with TestClient(create_app(config)) as client:
        assert client.get("/").status_code == 200
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
