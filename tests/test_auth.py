"""Logins: accounts, sessions, throttling, and who may see what (docs/m10-multi-user.md)."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_assessment, make_job
from fastapi.testclient import TestClient
from test_profiles import make_profile
from test_web import log_in

from jobsearcher import cli
from jobsearcher.auth import (
    ADMIN,
    MAX_FAILURES_USER,
    USER,
    Auth,
    AuthError,
    hash_password,
    verify_password,
)
from jobsearcher.config import load_config
from jobsearcher.ranking.ranker import Ranking
from jobsearcher.store import Store
from jobsearcher.web import create_app
from jobsearcher.web.app import SESSION_COOKIE

HX = {"HX-Request": "true"}
PASSWORD = "correct horse battery"


@pytest.fixture
def setup(tmp_path):
    """Two candidates (anna, bo); a client per test, logged out."""
    (tmp_path / "config.yaml").write_text("chat: {enabled: true}\n")
    make_profile(tmp_path, "anna", "Projektledare", ["Stockholm"], name="Anna")
    make_profile(tmp_path, "bo", "Controller", ["Göteborg"], name="Bo")
    config = load_config(tmp_path / "config.yaml")
    with TestClient(create_app(config, tmp_path / "config.yaml")) as client:
        yield config, client


def make_user(config, name, role=USER, profile="bo", password=PASSWORD):
    store = Store(config.db_path)
    auth = Auth(store.conn)
    user, _ = auth.create_user(name, role, profile)
    auth.set_password(user, password)
    return user


def test_passwords_hash_and_verify():
    stored = hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert verify_password(PASSWORD, stored)
    assert not verify_password("wrong password!", stored)
    assert not verify_password(PASSWORD, "garbage") and not verify_password(PASSWORD, "")


def test_every_page_needs_a_login_except_the_public_ones(setup):
    _, client = setup
    response = client.get("/jobs?view=tracked", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fjobs%3Fview%3Dtracked"
    assert client.get("/jobs", headers=HX).headers["HX-Redirect"].startswith("/login?")
    assert client.post("/jobs/x/state", data={"state": "applied"}, headers=HX).status_code == 401
    assert client.post("/settings/cvs", headers=HX).status_code == 401
    assert client.get("/healthz").text == "ok"
    assert client.get("/static/style.css").status_code == 200
    assert "Log in" in client.get("/login").text
    assert client.get("/nonexistent", follow_redirects=False).status_code == 303


def test_invite_link_sets_the_password_once(setup):
    config, client = setup
    store = Store(config.db_path)
    _, token = Auth(store.conn).create_user("bo", USER, "bo")
    assert "Set your password" in client.get(f"/invite/{token}").text
    mismatch = client.post(f"/invite/{token}", data={"password": PASSWORD, "confirm": "other"})
    assert mismatch.status_code == 400 and "The two passwords" in mismatch.text
    short = client.post(f"/invite/{token}", data={"password": "short", "confirm": "short"})
    assert short.status_code == 400 and "at least 12" in short.text
    done = client.post(
        f"/invite/{token}", data={"password": PASSWORD, "confirm": PASSWORD}, follow_redirects=False
    )
    assert done.status_code == 303 and SESSION_COOKIE in done.cookies
    assert client.get("/").status_code == 200  # logged in
    assert client.get(f"/invite/{token}").status_code == 404  # used up
    assert client.get("/invite/made-up").status_code == 404


def test_login_logout_and_safe_redirects(setup):
    config, client = setup
    make_user(config, "bo")
    wrong = client.post("/login", data={"username": "bo", "password": "nope nope nope"})
    unknown = client.post("/login", data={"username": "nobody", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert "Wrong username, password or code." in wrong.text and "Wrong username" in unknown.text
    ok = client.post(
        "/login",
        data={"username": "BO", "password": PASSWORD, "next": "//evil.example/x"},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/"  # not off-site
    cookie = ok.cookies[SESSION_COOKIE]
    assert "httponly" in ok.headers["set-cookie"].lower()
    assert "samesite=lax" in ok.headers["set-cookie"].lower()
    assert client.get("/jobs", follow_redirects=False).status_code == 200
    client.post("/logout")
    client.cookies.set(SESSION_COOKIE, cookie)  # the old cookie no longer works
    assert client.get("/jobs", follow_redirects=False).status_code == 303


def test_repeated_failures_lock_the_username_out(setup):
    config, client = setup
    make_user(config, "bo")
    for _ in range(MAX_FAILURES_USER):
        client.post("/login", data={"username": "bo", "password": "wrong password"})
    locked = client.post("/login", data={"username": "bo", "password": PASSWORD})
    assert locked.status_code == 401 and "Too many failed attempts" in locked.text
    store = Store(config.db_path)
    events = [r["event"] for r in store.conn.execute("SELECT event FROM audit_log")]
    assert events.count("login_failed") == MAX_FAILURES_USER and "login_throttled" in events


def test_sessions_expire_and_disabling_logs_out(setup):
    config, client = setup
    store = Store(config.db_path)
    auth = Auth(store.conn)
    user = log_in(client, config.db_path, "bo", USER, "bo")
    token = client.cookies[SESSION_COOKIE]
    assert auth.session_user(token) == user
    old = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    with store.conn:
        store.conn.execute("UPDATE sessions SET last_seen = ?", (old,))
    assert auth.session_user(token) is None  # idle too long, and removed
    log_in(client, config.db_path, "cecilia", USER, "bo")
    auth.set_disabled(auth.user_by_name("cecilia"), True)
    assert client.get("/jobs", follow_redirects=False).status_code == 303


def test_changing_the_password_keeps_this_session_and_ends_others(setup):
    config, client = setup
    make_user(config, "bo")
    other = TestClient(client.app)
    for c in (client, other):
        c.post("/login", data={"username": "bo", "password": PASSWORD})
    bad = client.post(
        "/account/password", data={"current": "wrong", "password": "x" * 12, "confirm": "x" * 12}
    )
    assert bad.status_code == 400 and "current password is wrong" in bad.text
    new = "a whole new passphrase"
    done = client.post(
        "/account/password", data={"current": PASSWORD, "password": new, "confirm": new}
    )
    assert "Password changed" in done.text
    assert client.get("/jobs", follow_redirects=False).status_code == 200
    assert other.get("/jobs", follow_redirects=False).status_code == 303


def test_a_user_sees_only_their_own_profile(setup):
    config, client = setup
    job = make_job(1, "Controller", location="Göteborg")
    Store(config.db_path).upsert_job(job)
    anna = Store(config.db_path, profile="anna")
    ranking = Ranking(job_id=job.id, input_hash="h", model="m", assessment=make_assessment())
    anna.save_ranking(job.id, "h", ranking.model_dump_json())
    anna.set_application(job.id, "applied", "Anna's note")
    log_in(client, config.db_path, "bo", USER, "bo")
    assert "Welcome, Bo" in client.get("/").text
    page = client.get(f"/jobs/{job.id}").text
    assert "Anna's note" not in page and "Not ranked yet" in page
    client.post(f"/jobs/{job.id}/state", data={"state": "shortlisted"}, headers=HX)
    assert Store(config.db_path, profile="bo").get_application(job.id).state == "shortlisted"
    assert anna.get_application(job.id).state == "applied"  # untouched
    cvs = client.get("/settings").text
    assert "bo.md" not in cvs and "master" in cvs and "profile.yaml" in cvs
    assert "config.yaml" not in cvs


def test_admin_only_pages(setup):
    config, client = setup
    log_in(client, config.db_path, "bo", USER, "bo")
    assert client.get("/status").status_code == 404
    assert client.get("/settings/files/config").status_code == 404
    assert client.post("/settings/files/config", data={"text": ""}, headers=HX).status_code == 404
    assert client.post("/chat/send", data={"message": "hi"}, headers=HX).status_code == 404
    assert "Ask Claude" not in client.get("/").text
    admin = TestClient(client.app)
    log_in(admin, config.db_path, "matthew", ADMIN, "anna")
    assert admin.get("/status").status_code == 200
    assert admin.get("/settings/files/config").status_code == 200
    assert "Ask Claude" in admin.get("/").text and "Welcome, Anna" in admin.get("/").text


def test_cross_site_writes_are_refused(setup):
    config, client = setup
    log_in(client, config.db_path, "bo", USER, "bo")
    evil = {**HX, "Origin": "https://evil.example"}
    assert client.post("/jobs/x/state", data={"state": "applied"}, headers=evil).status_code == 403
    login = client.post("/login", data={"username": "bo"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert login.status_code == 403
    same = {**HX, "Origin": "http://testserver"}
    assert client.post("/jobs/x/state", data={"state": "applied"}, headers=same).status_code == 404
    site = {**HX, "Sec-Fetch-Site": "same-site"}  # a sibling subdomain isn't this site
    assert client.post("/jobs/x/state", data={"state": "applied"}, headers=site).status_code == 403


def test_a_browsers_own_form_post_is_accepted(setup):
    """What a real browser sends for the invite and login forms: Origin "null" (the
    page's referrer policy hides it) but Sec-Fetch-Site same-origin."""
    config, client = setup
    _, token = Auth(Store(config.db_path).conn).create_user("bo", USER, "bo")
    browser = {"Origin": "null", "Sec-Fetch-Site": "same-origin"}
    done = client.post(
        f"/invite/{token}",
        data={"password": PASSWORD, "confirm": PASSWORD},
        headers=browser,
        follow_redirects=False,
    )
    assert done.status_code == 303
    forged = {"Origin": "null", "Sec-Fetch-Site": "cross-site"}
    assert client.post("/logout", headers=forged).status_code == 403
    login = client.post(
        "/login",
        data={"username": "bo", "password": PASSWORD},
        headers=browser,
        follow_redirects=False,
    )
    assert login.status_code == 303


def test_users_cli(setup, capsys):
    config, _ = setup
    args = ["--config", str(config.profiles_dir.parent / "config.yaml"), "users"]
    assert cli.main([*args, "add", "bo"]) == 2  # a user needs --profile
    assert cli.main([*args, "add", "bo", "--profile", "nope"]) == 2
    assert (
        cli.main([*args, "add", "bo", "--profile", "bo", "--url", "https://jobs.example.com"]) == 0
    )
    out = capsys.readouterr().out
    assert re.search(r"https://jobs\.example\.com/invite/[\w-]{30,}", out)
    assert cli.main([*args, "add", "matthew", "--admin"]) == 0
    assert cli.main([*args, "add", "Bo", "--profile", "bo"]) == 1  # names ignore case
    assert cli.main([*args, "disable", "bo"]) == 0
    capsys.readouterr()
    assert cli.main([*args, "list"]) == 0
    listing = capsys.readouterr().out
    assert re.search(r"bo\s+user\s+bo\s+disabled", listing)
    assert re.search(r"matthew\s+admin\s+anna\s+invited", listing)
    assert cli.main([*args, "invite", "nobody"]) == 1


def test_create_user_validates_names(setup):
    config, _ = setup
    auth = Auth(Store(config.db_path).conn)
    with pytest.raises(AuthError):
        auth.create_user("two words", USER, "bo")
    with pytest.raises(AuthError):
        auth.create_user("", USER, "bo")
