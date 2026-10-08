"""The preferences interview tab (jobsearcher/interview.py)."""

import yaml
from test_drafting import FakeLLM
from test_web import HX, web  # noqa: F401  (the `web` fixture)

from jobsearcher import interview
from jobsearcher.interview import Proposal, ProposedCompany, ProposedRole, Turn
from jobsearcher.llm import LLMError
from jobsearcher.ranking import load_ranking_config


def proposal(**kw):
    base = dict(
        target_roles=[ProposedRole(name="Project manager", aliases=["projektledare", "PM"])],
        situation="Lives in Uppsala.",
        seniority="Senior",
        languages="Swedish and English, fluent",
        likes=["Change work"],
        dislikes=["Night shifts"],
        dealbreakers=["Commuting over 90 minutes"],
        locations=["Uppsala", "Stockholm"],
        include_remote=True,
        keywords=[],
        exclude_keywords=["konsultuppdrag"],
        companies=[
            ProposedCompany(
                name="Acme AB", website="https://acme.se", reason=None, suggested=False
            ),
            ProposedCompany(name="Beta AB", website=None, reason="Like Acme.", suggested=True),
        ],
    )
    return Proposal(**(base | kw))


def test_saving_keeps_a_roles_other_settings_and_existing_companies():
    old = "target_roles:\n  - name: Project manager\n    aliases: [old]\n    exclude_occupations: [Bygg]\n"
    data = interview.ranking_data(old, proposal())
    [role] = data["target_roles"]
    assert role == {"name": "Project manager", "aliases": ["projektledare", "PM"],
                    "exclude_occupations": ["Bygg"]}  # fmt: skip
    assert data["preferences"]["dealbreakers"] == ["Commuting over 90 minutes"]
    assert interview.profile_data(proposal())["search"]["locations"] == ["Uppsala", "Stockholm"]

    companies = "companies:\n  # my favourites\n  - name: Acme AB\n    tags: [largest]\n"
    text, added = interview.companies_text(companies, proposal())
    assert added == ["Beta AB"]  # Acme is already there
    parsed = yaml.safe_load(text)["companies"]
    assert parsed[0] == {"name": "Acme AB", "tags": ["largest"]} and "# my favourites" in text
    assert parsed[1] == {"name": "Beta AB", "source": "interview", "tags": ["suggested"]}
    assert interview.companies_text("", proposal(companies=[]))[1] == []


def test_the_interview_from_start_to_saved_settings(web, monkeypatch):  # noqa: F811
    llm = FakeLLM(
        "opus",
        Turn(message="Hi! What are you looking for next?", done=False, topics_covered=[]),
        Turn(message="Which region?", done=False, topics_covered=["situation", "roles"]),
        proposal(),
    )
    monkeypatch.setattr("jobsearcher.web.app._interview_llm", lambda state, store: llm)
    page = web.client.get("/interview").text
    assert "Start in English" in page and "Börja på svenska" in page
    started = web.client.post("/interview/start", data={"language": "Swedish"}, headers=HX)
    assert started.headers["HX-Redirect"] == "/interview"
    assert "Interview in Swedish" in llm.calls[0]["system"]
    assert "Anna Andersson" in llm.calls[0]["context"]  # it starts from the CV
    assert "What are you looking for next?" in web.client.get("/interview").text

    web.client.post("/interview/answer", data={"text": "A PM role near Uppsala"}, headers=HX)
    assert "Candidate: A PM role near Uppsala" in llm.calls[1]["prompt"]
    page = web.client.get("/interview").text
    assert "Which region?" in page and 'class="chip good">roles' in page

    proposed = web.client.post("/interview/propose", headers=HX)
    assert proposed.headers["HX-Redirect"] == "/interview/review"
    review = web.client.get("/interview/review").text
    assert "projektledare" in review and "Beta AB" in review and "suggested" in review

    form = {
        "role-0-keep": "1", "role-0-name": "Project manager", "role-0-aliases": "projektledare\nPM",
        "situation": "Lives in Uppsala.", "seniority": "Senior", "languages": "sv, en",
        "likes": "Change work", "dislikes": "", "dealbreakers": "Long commutes",
        "locations": "Uppsala", "include_remote": "1", "keywords": "", "exclude_keywords": "",
        "company-0-add": "1", "company-0-name": "Acme AB", "company-0-suggested": "0",
        "company-0-website": "https://acme.se",
        "company-1-add": "0", "company-1-name": "Beta AB", "company-1-suggested": "1",
    }  # fmt: skip
    saved = web.client.post("/interview/apply", data=form, headers=HX).text
    assert "Saved your settings." in saved and "Added 1 companies: Acme AB" in saved
    rc = load_ranking_config(web.config.ranking_config)
    assert [r.name for r in rc.target_roles] == ["Project manager"]
    assert rc.preferences.dealbreakers == ["Long commutes"] and rc.preferences.seniority == "Senior"
    companies = yaml.safe_load(web.config.companies_config.read_text())["companies"]
    assert [c["name"] for c in companies] == ["Acme AB"]  # Beta was unticked
    assert "config.yaml (no profiles here)" in saved

    nothing = dict(form, **{"role-0-keep": "0"})
    assert (
        "Keep at least one target role."
        in web.client.post("/interview/apply", data=nothing, headers=HX).text
    )
    web.client.post("/interview/reset", headers=HX)
    assert "Start in English" in web.client.get("/interview").text


def test_model_problems_are_shown(web, monkeypatch):  # noqa: F811
    def no_model(state, store):
        raise LLMError("ANTHROPIC_API_KEY is not set (add it in Settings → Models and keys)")

    monkeypatch.setattr("jobsearcher.web.app._interview_llm", no_model)
    shown = web.client.post("/interview/start", data={"language": "English"}, headers=HX).text
    assert "ANTHROPIC_API_KEY is not set" in shown and "drafting model" in shown
    assert web.client.post("/interview/answer", data={"text": "x"}, headers=HX).status_code == 200
    assert web.client.get("/interview/review", follow_redirects=False).status_code == 303
