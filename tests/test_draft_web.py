import re
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_assessment, make_job
from test_drafting import FakeLLM, verdict
from test_drafting import content as _content
from test_web import CV as WEB_CV
from test_web import web  # noqa: F401  (the `web` fixture)

from jobsearcher import chat
from jobsearcher.drafting import service
from jobsearcher.drafting.core import Draft
from jobsearcher.models import Contact

HX = {"HX-Request": "true"}


def content(**kwargs):
    """A fake draft whose CV is the web fixture's master CV (so its figures are known)."""
    return _content(**({"cv": WEB_CV} | kwargs))


@pytest.fixture
def drafts(web, monkeypatch):  # noqa: F811
    """The web app with fake draft/check models and a fast renderer (no LibreOffice)."""
    monkeypatch.setattr("jobsearcher.drafting.render.docx_to_pdf", lambda path: None)
    made = {"drafter": [], "checker": []}

    def fake_llms(config, store):
        drafter = FakeLLM(
            "opus",
            *[content(letter=f"Dear Hiring Manager,\n\nLetter {n}.\n\nJenny") for n in range(1, 9)],
        )
        checker = FakeLLM("glm", *[verdict(("A claim", True)) for _ in range(8)])
        made["drafter"].append(drafter)
        return service.Llms(drafter, checker)

    monkeypatch.setattr(service, "make_llms", fake_llms)
    web.made = made
    return web


def wait(app, key, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = app.state.drafts.status(key)
        if status is not None and not status.active:
            return status
        time.sleep(0.02)
    raise AssertionError("draft didn't finish")


def add_job(web, n=1, **kw):  # noqa: F811
    job = web.add(
        make_job(n, f"HR Business Partner {n}", company="Acme AB", **kw), make_assessment(80, 70)
    )
    return job


def test_job_page_offers_a_draft_and_runs_it(drafts):
    job = add_job(drafts)
    page = drafts.client.get(f"/jobs/{job.id}").text
    assert (
        "Application draft" in page
        and "Nothing drafted yet" in page
        and "Draft application" in page
    )

    assert (
        drafts.client.post(f"/jobs/{job.id}/draft", data={}).status_code == 403
    )  # needs HX-Request
    started = drafts.client.post(
        f"/jobs/{job.id}/draft", data={"instructions": "Lead with M&A"}, headers=HX
    )
    assert started.status_code == 200
    assert "Writing the CV and letter" in started.text or "Waiting for its turn" in started.text
    assert (
        f'hx-get="/jobs/{job.id}/draft"' in started.text and 'hx-trigger="every 3s"' in started.text
    )
    wait(drafts, job.id)

    done = drafts.client.get(f"/jobs/{job.id}/draft").text
    assert "checked against your CVs" in done and "hx-trigger" not in done  # polling stops
    assert "Letter 1." in done and "Lead with M&amp;A" in done  # preview + instructions kept
    assert "Cover letter · Word" in done and "CV · Markdown" in done
    assert "Regenerate" in done and "PDF files need LibreOffice" in done
    # The job page shows the finished draft too.
    assert "Letter 1." in drafts.client.get(f"/jobs/{job.id}").text


def test_regenerating_keeps_older_versions(drafts):
    job = add_job(drafts)
    drafts.client.post(f"/jobs/{job.id}/draft", data={"instructions": "one"}, headers=HX)
    wait(drafts, job.id)
    drafts.client.post(f"/jobs/{job.id}/draft", data={"instructions": "two"}, headers=HX)
    wait(drafts, job.id)
    page = drafts.client.get(f"/jobs/{job.id}/draft").text
    assert page.count("Earlier versions") == 1
    older = re.search(r'hx-get="(/jobs/[^"?]+/draft\?v=\w+)"', page).group(1)
    assert "one" in drafts.client.get(older).text  # the first version's instructions


def test_flagged_claims_and_failures_are_shown(drafts, monkeypatch):
    job = add_job(drafts)

    def flagged_llms(config, store):
        bad = content(letter="Dear Hiring Manager,\n\nI hold a PMP.\n\nJenny")
        claim = verdict(("Holds a PMP", False))
        return service.Llms(FakeLLM("opus", bad, bad), FakeLLM("glm", claim, claim))

    monkeypatch.setattr(service, "make_llms", flagged_llms)
    drafts.client.post(f"/jobs/{job.id}/draft", data={}, headers=HX)
    wait(drafts, job.id)
    page = drafts.client.get(f"/jobs/{job.id}/draft").text
    assert "needs review" in page and "Holds a PMP" in page and "Please check these" in page

    def broken(config, store):
        raise chat.ChatUnavailable("x")

    from jobsearcher.llm import LLMError

    monkeypatch.setattr(
        service, "make_llms", lambda c, s: (_ for _ in ()).throw(LLMError("usage limit reached"))
    )
    other = add_job(drafts, 2)
    drafts.client.post(f"/jobs/{other.id}/draft", data={}, headers=HX)
    wait(drafts, other.id)
    failed = drafts.client.get(f"/jobs/{other.id}/draft").text
    assert "The draft didn't work" in failed and "usage limit reached" in failed
    assert "Draft application" in failed  # and she can try again


def test_downloads_and_the_preview_is_escaped(drafts, monkeypatch):
    job = add_job(drafts)
    evil = content(
        letter="Dear Hiring Manager,\n\n<script>alert(1)</script> **bold** text\n\nJenny"
    )
    monkeypatch.setattr(
        service,
        "make_llms",
        lambda c, s: service.Llms(FakeLLM("opus", evil), FakeLLM("glm", verdict())),
    )
    drafts.client.post(f"/jobs/{job.id}/draft", data={}, headers=HX)
    wait(drafts, job.id)
    page = drafts.client.get(f"/jobs/{job.id}/draft").text
    assert "<script>alert" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<strong>bold</strong>" in page

    version = re.search(r"/draft/(\w{16})/letter\.docx", page).group(1)
    word = drafts.client.get(f"/jobs/{job.id}/draft/{version}/letter.docx")
    assert word.status_code == 200 and word.content[:2] == b"PK"  # a real .docx (zip)
    assert (
        'filename="Anna-Andersson-Acme-AB-Cover-letter.docx"' in word.headers["content-disposition"]
    )
    md = drafts.client.get(f"/jobs/{job.id}/draft/{version}/cv.md")
    assert md.status_code == 200 and "Anna Andersson" in md.text
    assert (
        drafts.client.get(f"/jobs/{job.id}/draft/{version}/letter.pdf").status_code == 404
    )  # no PDF made
    assert drafts.client.get(f"/jobs/{job.id}/draft/{version}/../../x").status_code == 404
    assert drafts.client.get(f"/jobs/{job.id}/draft/{'0' * 16}/cv.md").status_code == 404
    assert drafts.client.get(f"/jobs/{job.id}/draft/{version}/secret.txt").status_code == 404


def set_state(web, job, state):  # noqa: F811
    return web.client.post(f"/jobs/{job.id}/state", data={"state": state}, headers=HX)


def test_shortlisting_starts_a_draft_within_the_daily_cap(drafts):
    jobs = [add_job(drafts, n) for n in (1, 2, 3)]
    drafts.state.ranking.get().drafting.max_drafts_per_day = 2
    set_state(drafts, jobs[0], "shortlisted")
    wait(drafts, jobs[0].id)
    latest = Draft.model_validate_json(drafts.store.list_drafts(jobs[0].id)[0]["data"])
    assert latest.trigger == "shortlist"

    set_state(drafts, jobs[0], "applied")
    set_state(drafts, jobs[0], "shortlisted")  # already drafted: nothing new
    assert len(drafts.store.list_drafts(jobs[0].id)) == 1

    set_state(drafts, jobs[1], "shortlisted")
    wait(drafts, jobs[1].id)
    set_state(drafts, jobs[2], "shortlisted")  # the cap of 2 automatic drafts is used up
    assert (
        drafts.state.drafts.status(jobs[2].id) is None
        and drafts.store.list_drafts(jobs[2].id) == []
    )

    set_state(drafts, jobs[2], "ignored")
    drafts.state.ranking.get().drafting.auto_on_shortlist = False
    drafts.state.ranking.get().drafting.max_drafts_per_day = 9
    set_state(drafts, jobs[2], "shortlisted")  # switched off in ranking.yaml
    assert drafts.state.drafts.status(jobs[2].id) is None


def test_dashboard_shows_draft_status_and_spontaneous_suggestions(drafts):
    job = drafts.add(make_job(1, "Top job"), make_assessment(90, 90))
    page = drafts.client.get("/").text
    assert f'href="/jobs/{job.id}#draft"' in page and "Draft application →" in page

    drafts.client.post(f"/jobs/{job.id}/draft", data={}, headers=HX)
    wait(drafts, job.id)
    assert "draft ready" in drafts.client.get("/").text

    now = datetime.now(UTC)
    companies = drafts.config.ranking_config.parent / "companies.yaml"
    companies.write_text(
        "companies:\n  - {name: Acme AB, website: https://acme.se, tags: [largest]}\n"
    )
    drafts.state.config.companies_config = companies
    drafts.store.save_news_items([{"id": "n1", "company": "acme-ab", "title": "Acme buys Beta", "url": "https://n/1",
        "domain": "di.se", "published_at": (now - timedelta(days=2)).isoformat(), "fetched_at": now.isoformat()}])  # fmt: skip
    drafts.store.save_signal("n1", "merger_acquisition", 88, "Acme is acquiring Beta.", "glm", "1")
    page = drafts.client.get("/").text
    assert "Companies worth a spontaneous application" in page and "Acme is acquiring Beta." in page
    assert 'href="/companies/acme-ab#draft"' in page


def test_company_page_and_spontaneous_draft(drafts, tmp_path):
    now = datetime.now(UTC)
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  - {name: Acme AB, website: https://acme.se}\n")
    drafts.state.config.companies_config = companies
    assert drafts.client.get("/companies/nope").status_code == 404

    empty = drafts.client.get("/companies/acme-ab").text
    assert "No recent news about Acme AB" in empty
    drafts.client.post("/companies/acme-ab/draft", data={}, headers=HX)
    wait(drafts, "company:acme-ab")
    assert (
        "No recent news" in drafts.client.get("/companies/acme-ab/draft").text
    )  # the failure, shown

    drafts.store.save_news_items([{"id": "n1", "company": "acme-ab", "title": "Acme buys Beta", "url": "https://n/1",
        "domain": "di.se", "published_at": (now - timedelta(days=2)).isoformat(), "fetched_at": now.isoformat()}])  # fmt: skip
    drafts.store.save_signal("n1", "merger_acquisition", 88, "Acme is acquiring Beta.", "glm", "1")
    drafts.web = None
    drafts.client.post("/companies/acme-ab/draft", data={}, headers=HX)
    wait(drafts, "company:acme-ab")
    page = drafts.client.get("/companies/acme-ab").text
    assert "Acme is acquiring Beta." in page and "Letter 1." in page
    version = re.search(r"/draft/(\w{16})/cv\.md", page).group(1)
    assert drafts.client.get(f"/companies/acme-ab/draft/{version}/cv.md").status_code == 200


def test_named_contacts_reach_the_letter_prompt(drafts):
    job = drafts.add(
        make_job(
            1,
            "HRBP",
            company="Acme",
            contacts=[Contact(name="Anna Svensson", role="HR-chef", provenance="llm:ad_text")],
        ),
        make_assessment(80, 70),
    )
    drafts.client.post(f"/jobs/{job.id}/draft", data={}, headers=HX)
    wait(drafts, job.id)
    assert "Dear Anna Svensson," in drafts.made["drafter"][0].calls[0]["prompt"]
    assert "addressed to Anna Svensson" in drafts.client.get(f"/jobs/{job.id}/draft").text


def test_chat_is_told_to_use_the_checked_drafting_path():
    assert "jobsearcher draft" in chat.SYSTEM_PROMPT and "checks every claim" in chat.SYSTEM_PROMPT
