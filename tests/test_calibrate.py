"""Calibration (jobsearcher/calibrate.py) and the dashboard's setup nudges."""

from conftest import make_assessment, make_job
from test_drafting import FakeLLM
from test_web import HX, web  # noqa: F401  (the `web` fixture)

from jobsearcher import calibrate, interview
from jobsearcher.ranking import load_ranking_config


def interview_done(web):  # noqa: F811
    web.store.save_interview(interview.Interview().to_json())
    interview.mark_applied(web.store)


def add_jobs(web, n, fit=lambda i: 90 - 3 * i, success=lambda i: 50):  # noqa: F811
    return [
        web.add(make_job(i + 1, f"Projektledare {i + 1}"), make_assessment(fit(i), success(i)))
        for i in range(n)
    ]


def test_dashboard_nudges_cv_then_interview(web):  # noqa: F811
    page = web.client.get("/").text
    assert "Start the interview" in page and "upload your CV" not in page
    interview_done(web)
    assert "Start the interview" not in web.client.get("/").text
    web.config.cv_path.unlink()
    assert "First, upload your CV." in web.client.get("/").text


def test_calibration_waits_for_the_interview_and_for_ranking(web):  # noqa: F811
    assert "Do the preferences interview first" in web.client.get("/calibrate").text
    interview_done(web)
    add_jobs(web, 5)
    web.add(
        make_job(99, "Projektledare old"), make_assessment(), input_hash="stale"
    )  # old settings
    waiting = web.client.get("/calibrate").text
    assert "5 of the 20 jobs" in waiting  # the stale one doesn't count
    refused = web.client.post("/calibrate/start", headers=HX).text
    assert "Waiting for ranking" in refused
    add_jobs(web, 30)
    assert "Start calibrating" in web.client.get("/calibrate").text


def test_pick_spreads_over_the_scores(web):  # noqa: F811
    from jobsearcher.web import views

    jobs = add_jobs(web, 40, fit=lambda i: 99 - 2 * i)
    rows = views.load_rows(web.store, web.state.row_context(), "all")
    picked = calibrate.pick(rows)
    assert len(picked) == 20 and picked[0] == jobs[0].id and picked[-1] == jobs[-1].id


def test_rating_results_weights_and_preferences(web, monkeypatch):  # noqa: F811
    interview_done(web)
    # The candidate cares about the chance of success, not fit: success predicts them.
    jobs = add_jobs(web, 24, fit=lambda i: 90 - 3 * i, success=lambda i: 20 + 3 * i)
    web.client.post("/calibrate/start", headers=HX)
    page = web.client.get("/calibrate").text
    assert "0 of 20 rated" in page and "Results appear after 12" in page
    round_ids = [c["job_id"] for c in web.store.calibrations()]
    by_id = {j.id: i for i, j in enumerate(jobs)}
    keys = [k for k, _, _ in calibrate.RATINGS]  # great .. no
    for job_id in round_ids[:14]:  # later jobs (higher success) rated higher
        rating = keys[min(4, (23 - by_id[job_id]) // 5)]
        note = "Too junior for me" if rating == "no" else ""
        web.client.post("/calibrate/rate", data={"job_id": job_id, "rating": rating, "note": note},
                        headers=HX)  # fmt: skip
    page = web.client.get("/calibrate").text
    assert "14 of 20 rated" in page and "How well the ranking agrees" in page
    assert "it often disagrees with you" in page and "The biggest disagreements" in page
    assert "Use these weights" in page and "chance of success 100%" in page

    change = calibrate.PreferenceChange(
        situation="", seniority="Senior", likes=["Stable employers"], dislikes=[],
        dealbreakers=["Junior roles"], explanation="Junior roles scored too high.",
    )  # fmt: skip
    llm = FakeLLM("opus", change)
    monkeypatch.setattr("jobsearcher.web.app._interview_llm", lambda state, store: llm)
    suggested = web.client.post("/calibrate/suggest", headers=HX).text
    assert (
        "Junior roles scored too high." in suggested
        and "Too junior for me" in llm.calls[0]["prompt"]
    )
    form = {"situation": "", "seniority": "Senior", "likes": "Stable employers",
            "dislikes": "", "dealbreakers": "Junior roles"}  # fmt: skip
    saved = web.client.post("/calibrate/preferences", data=form, headers=HX).text
    assert "Saved your preferences." in saved and "re-ranked" in saved
    prefs = load_ranking_config(web.config.ranking_config).preferences
    assert prefs.dealbreakers == ["Junior roles"] and prefs.seniority == "Senior"

    # New preferences make the rankings stale: calibration waits for re-ranking.
    assert "Waiting for ranking with your new settings" in web.client.get("/calibrate").text
    # Weights don't re-rank; they're saved as they are.
    saved = web.client.post("/calibrate/weights", data={"fit": "0", "success": "1"}, headers=HX)
    assert "Saved the new weights" in saved.text
    weights = load_ranking_config(web.config.ranking_config).weights
    assert (weights.fit, weights.success) == (0, 1)


def test_rating_guards(web):  # noqa: F811
    interview_done(web)
    add_jobs(web, 22)
    web.client.post("/calibrate/start", headers=HX)
    job_id = web.store.calibrations()[0]["job_id"]
    bad = web.client.post("/calibrate/rate", data={"job_id": job_id, "rating": "wow"}, headers=HX)
    assert bad.status_code == 400
    outside = web.client.post("/calibrate/rate", data={"job_id": "nope", "rating": "good"},
                              headers=HX)  # fmt: skip
    assert outside.status_code == 404
    web.client.post("/calibrate/reset", headers=HX)
    assert (
        web.store.calibrations() == [] and "Start calibrating" in web.client.get("/calibrate").text
    )
