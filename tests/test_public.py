"""The internet-facing listener (behind Caddy): what changes for requests on it."""

import socket
import time

import pytest
from fastapi.testclient import TestClient
from test_auth import HX, PASSWORD, make_user
from test_profiles import make_profile
from test_web import log_in

from jobsearcher import cli
from jobsearcher.auth import ADMIN, MAX_FAILURES_IP, USER, Auth, totp_code
from jobsearcher.config import load_config
from jobsearcher.store import Store
from jobsearcher.web import create_app
from jobsearcher.web.app import SECURITY_HEADERS, SESSION_COOKIE


def app_for(tmp_path, public_port):
    (tmp_path / "config.yaml").write_text("chat: {enabled: true}\n")
    if not (tmp_path / "profiles").exists():
        make_profile(tmp_path, "anna", "Projektledare", ["Stockholm"], name="Anna")
    config = load_config(tmp_path / "config.yaml")
    config.web.public_port = public_port
    return config, create_app(config, tmp_path / "config.yaml")


@pytest.fixture
def public(tmp_path):
    """TestClient's requests arrive on port 80: make that the public listener."""
    config, app = app_for(tmp_path, 80)
    with TestClient(app) as client:
        yield config, client


def with_totp(config, name):
    """An admin with a password and two-factor codes; returns the secret."""
    make_user(config, name, ADMIN, "anna")
    auth = Auth(Store(config.db_path).conn)
    user = auth.user_by_name(name)
    secret = auth.start_totp(user)
    auth.confirm_totp(user, totp_code(secret, int(time.time() // 30)))
    return secret


def test_security_headers_on_every_response(public):
    _, client = public
    for response in (client.get("/login"), client.get("/jobs", follow_redirects=False)):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value
    assert "script-src 'self';" in SECURITY_HEADERS["Content-Security-Policy"]


def test_admins_need_two_factor_codes_from_the_internet(public):
    config, client = public
    make_user(config, "matthew", ADMIN, "anna")
    refused = client.post("/login", data={"username": "matthew", "password": PASSWORD})
    assert refused.status_code == 401 and "two-factor codes" in refused.text
    log_in(client, config.db_path, "lan-admin", ADMIN, "anna")  # a session without them
    assert client.get("/").status_code == 403
    make_user(config, "bo", USER, "anna")  # ordinary users don't need them
    bo = TestClient(client.app)
    assert bo.post("/login", data={"username": "bo", "password": PASSWORD}).status_code == 200


def test_no_chat_from_the_internet_and_secure_cookies(public):
    config, client = public
    secret = with_totp(config, "matthew")
    code = totp_code(secret, int(time.time() // 30) + 1)
    response = client.post(
        "/login",
        data={"username": "matthew", "password": PASSWORD, "code": code},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "secure" in response.headers["set-cookie"].lower()
    client.cookies.set(SESSION_COOKIE, response.cookies[SESSION_COOKIE])
    page = client.get("/")
    assert page.status_code == 200 and "Ask Claude" not in page.text
    assert client.post("/chat/send", data={"message": "hi"}, headers=HX).status_code == 404
    assert client.get("/chat/x/stream").status_code == 404
    assert client.get("/status").status_code == 200  # other admin pages work


def test_forwarded_address_counts_for_throttling_only_through_the_proxy(public, tmp_path):
    config, client = public
    make_user(config, "bo", USER, "anna")
    for n in range(MAX_FAILURES_IP):  # different usernames: only the address adds up
        client.post(
            "/login",
            data={"username": f"x{n}", "password": "wrong password!"},
            headers={"X-Forwarded-For": "9.9.9.9, 1.2.3.4"},  # the client wrote 9.9.9.9
        )
    login = {"username": "bo", "password": PASSWORD}
    blocked = client.post("/login", data=login, headers={"X-Forwarded-For": "1.2.3.4"})
    assert blocked.status_code == 401 and "Too many" in blocked.text
    other = client.post(
        "/login", data=login, headers={"X-Forwarded-For": "5.6.7.8"}, follow_redirects=False
    )
    assert other.status_code == 303
    ips = {r["ip"] for r in Store(config.db_path).conn.execute("SELECT ip FROM audit_log")}
    assert "1.2.3.4" in ips and "9.9.9.9" not in ips


def test_the_home_network_listener_is_unchanged(tmp_path):
    config, app = app_for(tmp_path, 8081)  # TestClient arrives on 80: not public
    with TestClient(app) as client:
        log_in(client, config.db_path, "matthew", ADMIN, "anna")  # no two-factor codes
        assert "Ask Claude" in client.get("/").text
        client.post("/login", data={"username": "a", "password": "b"},
                    headers={"X-Forwarded-For": "1.2.3.4"})  # fmt: skip
        ips = {r["ip"] for r in Store(config.db_path).conn.execute("SELECT ip FROM audit_log")}
        assert "1.2.3.4" not in ips  # not trusted here


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_cmd_web_listens_on_both_ports(monkeypatch, tmp_path):
    import uvicorn

    ran = []

    class FakeServer:
        def __init__(self, config):
            self.config = config

        def run(self, sockets):
            ran.append((self.config, [s.getsockname() for s in sockets]))
            for s in sockets:
                s.close()

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    config, _ = app_for(tmp_path, free_port())
    lan = free_port()
    args = cli.argparse.Namespace(host="127.0.0.1", port=lan, reload=False, config=None)
    assert cli.cmd_web(config, args) == 0
    [(server_config, bound)] = ran
    assert bound == [("127.0.0.1", lan), ("127.0.0.1", config.web.public_port)]
    assert server_config.proxy_headers is False
