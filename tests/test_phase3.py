"""Each candidate's own models, keys and budget; the admin's tools; two-factor codes."""

import stat
import time

import pytest
import yaml
from fastapi.testclient import TestClient
from test_auth import HX, PASSWORD, make_user, setup  # noqa: F401  (the `setup` fixture)
from test_profiles import make_profile
from test_web import log_in

from jobsearcher import cli
from jobsearcher.auth import ADMIN, USER, Auth, AuthError, totp_code, totp_match
from jobsearcher.config import LLMConfig, Provider, load_config, save_secret
from jobsearcher.llm import BudgetTracker, LLMError, LLMUsage, make_llm
from jobsearcher.llm.ratelimit import limiter_for
from jobsearcher.store import Store


@pytest.fixture
def keyed(tmp_path, monkeypatch):
    """anna uses the server's keys (.env); bo brings his own."""
    monkeypatch.setenv("ZAI_API_KEY", "server-zai-key")
    (tmp_path / "config.yaml").write_text(
        "llm:\n  server_key_profiles: [anna]\n"
        "  ranking: {provider: zai, model: glm-5.3-flash}\n"
        "  drafting: {provider: claude_code, model: opus}\n"
        "  grounding_fallback: {provider: claude_code, model: haiku}\n"
    )
    make_profile(tmp_path, "anna", "Projektledare", ["Stockholm"])
    make_profile(tmp_path, "bo", "Controller", ["Göteborg"])
    return load_config(tmp_path / "config.yaml")


def test_server_key_profiles_use_env_and_others_their_own_keys(keyed):
    anna, bo = keyed.for_profile("anna"), keyed.for_profile("bo")
    assert not anna.own_keys and anna.api_key("ZAI_API_KEY") == "server-zai-key"
    assert bo.own_keys and bo.api_key("ZAI_API_KEY") is None  # never the server's
    assert bo.llm.grounding_fallback is None  # the admin's claude_code isn't inherited
    assert anna.llm.grounding_fallback is not None
    assert "api_keys" not in bo.model_dump() and "zai" not in repr(bo.api_keys)


def test_make_llm_refuses_admin_providers_and_missing_keys(keyed):
    bo = keyed.for_profile("bo")
    tracker = BudgetTracker(Store(":memory:", profile="bo"), bo.llm)
    with pytest.raises(LLMError, match="admin's own subscription"):
        make_llm(bo, "drafting", tracker)
    with pytest.raises(LLMError, match=r"ZAI_API_KEY is not set \(add it in Settings"):
        make_llm(bo, "ranking", tracker)
    save_secret(bo.secrets_file, "ZAI_API_KEY", "bos-own-key")
    bo = keyed.for_profile("bo")
    llm = make_llm(bo, "ranking", tracker)
    assert llm.client.client.api_key == "bos-own-key"
    anna = keyed.for_profile("anna")
    assert make_llm(anna, "ranking", tracker).client.client.api_key == "server-zai-key"


def test_secrets_file_is_private_and_validated(keyed):
    path = keyed.for_profile("bo").secrets_file
    save_secret(path, "ZAI_API_KEY", " key-1 ")
    save_secret(path, "MOONSHOT_API_KEY", "key-2")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert keyed.for_profile("bo").api_keys == {"ZAI_API_KEY": "key-1", "MOONSHOT_API_KEY": "key-2"}
    save_secret(path, "ZAI_API_KEY", None)
    assert keyed.for_profile("bo").api_keys == {"MOONSHOT_API_KEY": "key-2"}
    with pytest.raises(ValueError):
        save_secret(path, "PATH", "x")
    with pytest.raises(ValueError):
        save_secret(path, "ZAI_API_KEY", "two\nlines")


def test_profile_models_and_budget_override_config_yaml(keyed, tmp_path):
    folder = tmp_path / "profiles" / "bo"
    settings = yaml.safe_load((folder / "profile.yaml").read_text())
    settings["llm"] = {
        "ranking": {"provider": "moonshot", "model": "kimi-k3"},
        "monthly_budget_usd": 5,
    }
    (folder / "profile.yaml").write_text(yaml.safe_dump(settings))
    bo = keyed.for_profile("bo")
    assert (bo.llm.ranking.provider, bo.llm.ranking.model) == (Provider.MOONSHOT, "kimi-k3")
    assert bo.llm.monthly_budget_usd == 5 and bo.llm.drafting.model == "opus"
    assert keyed.for_profile("anna").llm.monthly_budget_usd == 20


def test_budgets_and_rate_limits_are_per_profile(keyed):
    db = keyed.db_path
    anna = BudgetTracker(Store(db, profile="anna"), LLMConfig(monthly_budget_usd=1))
    bo = BudgetTracker(Store(db, profile="bo"), LLMConfig(monthly_budget_usd=1))
    anna.record(LLMUsage(model="kimi-k3", input_tokens=400_000, output_tokens=0), "ranking")
    assert anna.month_to_date() == pytest.approx(1.2) and bo.month_to_date() == 0
    bo.check("ranking")  # anna's spending doesn't stop bo
    assert limiter_for("zai", 30, "key-a") is not limiter_for("zai", 30, "key-b")
    assert limiter_for("zai", 30, "key-a") is limiter_for("zai", 30, "key-a")


def test_models_page_saves_models_and_keys(keyed, monkeypatch):
    with TestClient(cli_app(keyed)) as client:
        log_in(client, keyed.db_path, "bo", USER, "bo")
        page = client.get("/settings/models").text
        assert "API keys" in page and "claude_code" not in page.split("Provider")[1]
        bad = client.post(
            "/settings/models",
            data={"drafting-provider": "claude_code", "drafting-model": "opus"},
            headers=HX,
        )
        assert "available to this profile" in bad.text
        ok = client.post(
            "/settings/models",
            data={
                "ranking-provider": "zai",
                "ranking-model": "glm-5.3-flash",
                "ranking-extra": '{"reasoning_effort": "low"}',
                "drafting-provider": "anthropic",
                "drafting-model": "claude-sonnet-5-5",
                "monthly_budget_usd": "4",
            },
            headers=HX,
        )
        assert "Saved." in ok.text
        bo = keyed.for_profile("bo")
        assert bo.llm.ranking.extra_body == {"reasoning_effort": "low"}
        assert bo.llm.drafting.provider == Provider.ANTHROPIC and bo.llm.monthly_budget_usd == 4

        client.post(
            "/settings/keys", data={"env": "ZAI_API_KEY", "key": "sk-secret-abcd"}, headers=HX
        )
        page = client.get("/settings/models").text
        assert "…abcd" in page and "sk-secret" not in page
        assert keyed.for_profile("bo").api_keys["ZAI_API_KEY"] == "sk-secret-abcd"
        client.post("/settings/keys", data={"env": "ZAI_API_KEY", "remove": "1"}, headers=HX)
        assert keyed.for_profile("bo").api_keys == {}

        class Fake:
            def complete(self, **kw):
                from jobsearcher.llm import LLMResult

                usage = LLMUsage(model="m", input_tokens=5, output_tokens=2)
                return LLMResult(text="{}", usage=usage, parsed=None)

        monkeypatch.setattr("jobsearcher.llm.make_llm", lambda config, role, tracker: Fake())
        assert "Both models work." in client.post("/settings/models/test", headers=HX).text

        anna = TestClient(client.app)
        log_in(anna, keyed.db_path, "anna", USER, "anna")
        assert "uses the server's keys" in anna.get("/settings/models").text
        refused = anna.post("/settings/keys", data={"env": "ZAI_API_KEY", "key": "x"}, headers=HX)
        assert refused.status_code == 404


def cli_app(config):
    from jobsearcher.web import create_app

    return create_app(config, config.profiles_dir.parent / "config.yaml")


def test_admin_switches_profiles_and_manages_accounts(keyed):
    with TestClient(cli_app(keyed)) as client:
        log_in(client, keyed.db_path, "matthew", ADMIN, "anna")
        assert 'name="profile"' in client.get("/").text  # the switcher
        assert client.post("/admin/act-as", data={"profile": "nope"}, headers=HX).status_code == 400
        client.post("/admin/act-as", data={"profile": "bo"}, headers=HX)
        assert "<option selected>bo</option>" in client.get("/").text

        added = client.post(
            "/admin/users", data={"username": "bo", "role": "user", "profile": "bo"}, headers=HX
        ).text
        assert "Link for bo" in added and "/invite/" in added
        assert (
            "already exists"
            in client.post(
                "/admin/users", data={"username": "BO", "profile": "bo"}, headers=HX
            ).text
        )
        assert "Link for bo" in client.post("/admin/users/bo/invite", headers=HX).text
        client.post("/admin/users/bo/disable", headers=HX)
        assert "disabled" in client.get("/admin/users").text
        assert (
            "disable your own account"
            in client.post("/admin/users/matthew/disable", headers=HX).text
        )

        bo = TestClient(client.app)
        log_in(bo, keyed.db_path, "cecilia", USER, "bo")
        assert bo.get("/admin/users").status_code == 404
        assert bo.post("/admin/act-as", data={"profile": "anna"}, headers=HX).status_code == 404
        assert 'hx-post="/admin/act-as"' not in bo.get("/").text


def test_two_factor_codes(setup):  # noqa: F811
    config, client = setup
    make_user(config, "matthew", ADMIN, "anna")
    client.post("/login", data={"username": "matthew", "password": PASSWORD})
    page = client.post("/account/2fa/start").text
    assert "<svg" in page and "Switch on" in page
    auth = Auth(Store(config.db_path).conn)
    user = auth.user_by_name("matthew")
    secret = auth.pending_totp(user)
    assert (
        "match. Check the phone"
        in client.post("/account/2fa/confirm", data={"code": "000000"}).text
    )
    step = int(time.time() // 30)
    on = client.post("/account/2fa/confirm", data={"code": totp_code(secret, step)})
    assert "Two-factor codes are on" in on.text and auth.user_by_name("matthew").has_totp

    other = TestClient(client.app)
    login = {"username": "matthew", "password": PASSWORD}
    assert other.post("/login", data=login).status_code == 401  # no code
    used = totp_code(secret, step)  # used to switch it on: can't log in with it again
    assert other.post("/login", data={**login, "code": used}).status_code == 401
    fresh = totp_code(secret, step + 1)
    assert (
        other.post("/login", data={**login, "code": fresh}, follow_redirects=False).status_code
        == 303
    )
    with pytest.raises(AuthError):
        auth.disable_totp(user, "wrong password", totp_code(secret, step))
    assert totp_match(secret, totp_code(secret, step - 1), after=step - 2) == step - 1
    assert totp_match(secret, totp_code(secret, step - 5)) is None  # too old

    args = ["--config", str(config.profiles_dir.parent / "config.yaml"), "users"]
    assert cli.main([*args, "reset-2fa", "matthew"]) == 0
    assert not auth.user_by_name("matthew").has_totp
