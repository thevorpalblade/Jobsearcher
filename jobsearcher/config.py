"""Configuration: `config.yaml` for settings, environment variables for secrets."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


class SearchConfig(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    # Free-text locations matched against municipality/region/city (case-insensitive).
    # Empty means everywhere.
    locations: list[str] = Field(default_factory=list)
    include_remote: bool = True
    # Also search for every target role name/alias listed in ranking.yaml.
    use_target_roles: bool = True
    # Every run fetches all currently open matching ads, so a job that stops appearing
    # has been filled or withdrawn. Days unseen (or past deadline) before marking it expired.
    expire_after_days: int = 3


HONEST_USER_AGENT = "jobsearcher/0.1 (personal job search tool)"
CHROME_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class CrawlConfig(BaseModel):
    """How the crawler presents itself. The defaults are the polite ones; a personal,
    low-volume setup may choose a browser user agent and ignore robots.txt (per-host
    pacing always applies)."""

    # "chrome", "honest", or a literal User-Agent string.
    user_agent: str = "honest"
    respect_robots: bool = True

    @property
    def user_agent_string(self) -> str:
        return {"chrome": CHROME_USER_AGENT, "honest": HONEST_USER_AGENT}.get(
            self.user_agent, self.user_agent
        )


class JobSpyConfig(BaseModel):
    """LinkedIn, Indeed (and Glassdoor/Google) via JobSpy, the `jobspy` extra."""

    sites: list[Literal["linkedin", "indeed", "glassdoor", "google"]] = Field(
        default_factory=list
    )  # empty: off
    location: str = "Sweden"  # the search.locations filter then keeps your cities
    country_indeed: str = "sweden"
    # These sites only return recent ads, so each run asks for the last `hours_old`.
    hours_old: int = 168
    results_per_search: int = 20
    # Not seeing a JobSpy ad again doesn't mean it's filled (older ads just aren't
    # asked for), so these jobs expire by age instead.
    max_age_days: int = 30
    pause_s: float = 5.0  # between searches on one site, against rate limiting


class SourcesConfig(BaseModel):
    platsbanken: bool = True
    jobtech_links: bool = True
    jobspy: JobSpyConfig = Field(default_factory=JobSpyConfig)
    # Careers sites (ATS feeds) of the companies in companies.yaml.
    companies: bool = True


class CompaniesSettings(BaseModel):
    # Re-detect each company's ATS after this many days (careers sites rarely move).
    redetect_after_days: int = 7
    # Pause between requests to the same domain, to crawl politely.
    min_request_interval_s: float = 1.0
    # News signals: how often to fetch and classify news (days), and how far back.
    signals_every_days: int = 7
    news_days: int = 30
    # auto: Google News when crawl.respect_robots is off (its robots.txt disallows
    # the RSS), else GDELT.
    news_source: Literal["auto", "google_news", "gdelt"] = "auto"


class Provider(StrEnum):
    MOONSHOT = "moonshot"  # Kimi models, OpenAI-compatible API
    ANTHROPIC = "anthropic"  # Claude models, pay-per-token API key
    NVIDIA = "nvidia"  # models on NVIDIA's serverless endpoints (e.g. GLM), OpenAI-compatible API
    ZAI = "zai"  # GLM models from Z.ai (their maker), pay per token, OpenAI-compatible API
    OLLAMA = "ollama"  # a model on this machine through Ollama's OpenAI-compatible API
    CLAUDE_CODE = "claude_code"  # Claude via the Claude Code CLI on a Claude subscription


class ModelRole(BaseModel):
    provider: Provider
    model: str
    # anthropic / claude_code: effort level (low | medium | high | xhigh | max).
    # Leave unset for models that don't support it (e.g. claude-haiku-4-5).
    effort: str | None = None
    max_tokens: int = 16000
    # Seconds before a call is abandoned (claude_code CLI; OpenAI-compatible requests).
    timeout_s: float = 600
    # moonshot / nvidia: automatic retries of a failed request (each may wait timeout_s).
    max_retries: int = 2
    # moonshot / nvidia: extra provider-specific request fields, e.g.
    # {"thinking": {"type": "disabled"}} to stop GLM reasoning before it answers.
    extra_body: dict[str, Any] = Field(default_factory=dict)
    # moonshot / nvidia: have the server enforce the JSON schema (response_format
    # json_schema). Without it the model only gets the schema as text, and GLM
    # sometimes echoed the schema back instead of filling it in.
    enforce_schema: bool = False
    # How many requests may run at once (ranking). Raise it for slow, queued
    # providers such as NVIDIA's free tier; keep 1 for claude_code.
    max_parallel: int = Field(default=1, ge=1)


class ModelPrice(BaseModel):
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float | None = None  # defaults to `input` when the provider doesn't bill writes


# List prices, Sep 2026. Override or extend under `llm.prices` in config.yaml.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "kimi-k2.6": ModelPrice(input=0.95, output=4.00, cache_read=0.16),
    "kimi-k3": ModelPrice(input=3.00, output=15.00, cache_read=0.30),
    # Z.ai's own API (the NVIDIA-hosted GLM is free; its price is set in config.yaml).
    "glm-5.3-flash": ModelPrice(input=0.15, output=0.50, cache_read=0.03),
    "glm-4.7-flash": ModelPrice(input=0, output=0, cache_read=0),
    "glm-4.5-flash": ModelPrice(input=0, output=0, cache_read=0),
    "claude-haiku-4-5": ModelPrice(input=1.00, output=5.00, cache_read=0.10, cache_write=1.25),
    "claude-sonnet-5-5": ModelPrice(input=2.00, output=10.00, cache_read=0.20, cache_write=2.50),
    "claude-opus-5-5": ModelPrice(input=4.00, output=20.00, cache_read=0.20, cache_write=5.00),
    # Possible targets of Anthropic's server-side refusal fallback.
    "claude-opus-5": ModelPrice(input=5.00, output=25.00, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-8": ModelPrice(input=5.00, output=25.00, cache_read=0.50, cache_write=6.25),
}


class LLMConfig(BaseModel):
    # The independent model that checks drafts against the CVs; default: the ranking model,
    # with a short timeout. `grounding_fallback` answers instead when it is too slow or down.
    grounding: ModelRole | None = None
    grounding_fallback: ModelRole | None = None
    ranking: ModelRole = Field(
        default_factory=lambda: ModelRole(provider=Provider.MOONSHOT, model="kimi-k2.6")
    )
    drafting: ModelRole = Field(
        default_factory=lambda: ModelRole(provider=Provider.MOONSHOT, model="kimi-k3")
    )
    moonshot_base_url: str = "https://api.moonshot.ai/v1"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    ollama_base_url: str = "http://localhost:11434/v1"
    zai_base_url: str = "https://api.z.ai/api/paas/v4"
    # NVIDIA's free hosted API allows about 40 requests a minute per key, shared by every
    # model, and answers 429 beyond it. Every NVIDIA request (retries included, from
    # ranking, news classification and draft checks alike) is spaced to stay under this;
    # 0 turns the limiter off (e.g. on a paid plan).
    nvidia_requests_per_minute: int = 30
    # Moonshot's request limit depends on the account's tier (3 a minute for a new account);
    # 0 = no limiter.
    moonshot_requests_per_minute: int = 0
    # Z.ai doesn't publish its paid limits; 0 = no limiter (a 429 still pauses and retries).
    zai_requests_per_minute: int = 0
    # Profiles that use the keys in .env and may use claude_code and ollama (the admin's
    # own subscription and GPU). Other profiles bring their own keys (secrets.env in
    # their folder) and use pay-per-token providers. A setup without profiles uses .env.
    server_key_profiles: list[str] = Field(default_factory=list)
    monthly_budget_usd: float = 20.0
    # Drafting pauses once this share of the monthly budget is spent; ranking at 100%.
    drafting_budget_share: float = 0.8
    prices: dict[str, ModelPrice] = Field(default_factory=dict)

    def price_for(self, model: str) -> ModelPrice | None:
        return self.prices.get(model) or DEFAULT_PRICES.get(model)


class ScheduleConfig(BaseModel):
    # Local time (HH:MM) for the daily run when using `jobsearcher daemon`.
    daily_at: str = "06:00"
    timezone: str = "Europe/Stockholm"
    # When ranking or news classification stops because the provider keeps failing (a rate
    # limit, an outage), the daemon tries again after this many minutes, up to `retries`
    # times (and never into the next daily run). 0 retries = wait for the next daily run.
    retry_minutes: int = 30
    retries: int = 16


class WebConfig(BaseModel):
    # `jobsearcher web` defaults; the Docker service passes --host 0.0.0.0.
    host: str = "127.0.0.1"
    port: int = 8080
    # Who the dashboard greets ("Welcome, Jenny"); plain "Welcome" when empty.
    user_name: str = ""
    # Host names (besides IP addresses, localhost and one-word or .local/.lan names)
    # the chat accepts requests for; "*" turns the check off. Guards the chat against
    # DNS rebinding (on top of the login).
    allowed_hosts: list[str] = Field(default_factory=list)
    # The internet-facing listener (docs/m10-multi-user.md, phase 4): `jobsearcher web`
    # also listens on 127.0.0.1:<public_port>, for a reverse proxy (Caddy) on this
    # machine. Requests that come in on it are public: no chat, admins need two-factor
    # codes, cookies are Secure, and the proxy's X-Forwarded-For is trusted. None: off.
    public_port: int | None = None


class ChatConfig(BaseModel):
    """The dashboard's chat with Claude Code, run in this repository."""

    # Off unless asked for: with no login and full access, anyone who can open the
    # page can make Claude Code act on this machine.
    enabled: bool = False
    model: str = "opus"
    effort: str | None = "medium"
    # bypassPermissions: no prompts (there's nobody at the terminal to answer them).
    permission_mode: str = "bypassPermissions"
    timeout_s: float = 1800
    # The checkout Claude works in; default: the folder holding config.yaml.
    workdir: Path | None = None


# The profile of a setup without a `profiles/` folder: its CVs, ranking.yaml,
# companies.yaml and search settings are the paths and section in config.yaml.
DEFAULT_PROFILE = "default"
PROFILE_FILE = "profile.yaml"


# Only profiles in llm.server_key_profiles may use these: the admin's own Claude
# subscription and GPU.
ADMIN_PROVIDERS = frozenset({Provider.CLAUDE_CODE, Provider.OLLAMA})
# The environment variable (in .env, or a profile's secrets.env) with each provider's key.
KEY_ENV = {
    Provider.MOONSHOT: "MOONSHOT_API_KEY",
    Provider.ANTHROPIC: "ANTHROPIC_API_KEY",
    Provider.NVIDIA: "NVIDIA_API_KEY",
    Provider.ZAI: "ZAI_API_KEY",
}
SECRETS_FILE = "secrets.env"


class ProfileLLM(BaseModel):
    """A profile's own models and budget; anything unset comes from config.yaml."""

    ranking: ModelRole | None = None
    drafting: ModelRole | None = None
    grounding: ModelRole | None = None
    grounding_fallback: ModelRole | None = None
    monthly_budget_usd: float | None = None


class ProfileSettings(BaseModel):
    """profiles/<slug>/profile.yaml: one candidate's own settings."""

    # Who the dashboard greets; falls back to web.user_name.
    name: str = ""
    # Their keywords, excluded words and region. expire_after_days stays global
    # (config.yaml), since the job pool is shared.
    search: SearchConfig = Field(default_factory=SearchConfig)
    llm: ProfileLLM = Field(default_factory=ProfileLLM)


def read_secrets(path: Path) -> dict[str, str]:
    """KEY=value lines of a profile's secrets.env."""
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if sep and key and not key.startswith("#") and value:
            out[key] = value
    return out


def save_secret(path: Path, env: str, value: str | None) -> None:
    """Set (or with None, remove) one key in a secrets.env, readable by its owner only."""
    if env not in KEY_ENV.values():
        raise ValueError(f"Unknown key {env!r}")
    if value is not None and (not value.strip() or "\n" in value or len(value) > 500):
        raise ValueError("That doesn't look like an API key.")
    secrets = read_secrets(path)
    if value is None:
        secrets.pop(env, None)
    else:
        secrets[env] = value.strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text("".join(f"{k}={v}\n" for k, v in sorted(secrets.items())))


def load_profile_settings(path: Path) -> ProfileSettings:
    raw = yaml.safe_load(path.read_text()) if path.is_file() else {}
    return ProfileSettings.model_validate(raw or {})


class Config(BaseModel):
    search: SearchConfig = Field(default_factory=SearchConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    data_dir: Path = Path("data")
    # Relative paths are resolved against the directory containing config.yaml.
    ranking_config: Path = Path("ranking.yaml")
    cv_path: Path = Path("cvs/master.md")
    companies_config: Path = Path("companies.yaml")
    companies: CompaniesSettings = Field(default_factory=CompaniesSettings)
    # One folder per candidate (docs/m10-multi-user.md); load_config defaults it to
    # profiles/ next to config.yaml. Without it, the setup has one profile,
    # DEFAULT_PROFILE, made of the paths above.
    profiles_dir: Path | None = None
    # Which profile this config is for, and its own API keys: set by for_profile(), not
    # in config.yaml, and never written out.
    profile: str = DEFAULT_PROFILE
    own_keys: bool = Field(default=False, exclude=True)
    api_keys: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)

    def api_key(self, env: str) -> str | None:
        """A provider's key: the profile's own (secrets.env), or for the admin's
        profiles (llm.server_key_profiles) and setups without profiles, .env's."""
        return self.api_keys.get(env) if self.own_keys else os.environ.get(env)

    def provider_allowed(self, provider: Provider) -> bool:
        return not (self.own_keys and provider in ADMIN_PROVIDERS)

    def profile_slugs(self) -> list[str]:
        """Every profile: the folders in profiles/ with a profile.yaml, by name."""
        if self.profiles_dir is None or not self.profiles_dir.is_dir():
            return [DEFAULT_PROFILE]
        slugs = sorted(d.name for d in self.profiles_dir.iterdir() if (d / PROFILE_FILE).is_file())
        return slugs or [DEFAULT_PROFILE]

    def for_profile(self, slug: str | None = None) -> Config:
        """This config with one profile's files and search settings (default: the first
        profile). Everything else, e.g. llm, sources and schedule, stays global."""
        slugs = self.profile_slugs()
        slug = slug or slugs[0]
        if slug not in slugs:
            raise ValueError(f"No profile {slug!r} (profiles: {', '.join(slugs)})")
        if slug == DEFAULT_PROFILE or self.profiles_dir is None:
            return self.model_copy(update={"profile": slug})
        folder = self.profiles_dir / slug
        settings = load_profile_settings(folder / PROFILE_FILE)
        search = settings.search.model_copy(
            update={"expire_after_days": self.search.expire_after_days}
        )
        web = self.web.model_copy(update={"user_name": settings.name or self.web.user_name})
        own_keys = slug not in self.llm.server_key_profiles
        overrides = settings.llm.model_dump(exclude_none=True)
        llm = self.llm.model_copy(
            update={k: getattr(settings.llm, k) for k in overrides}  # models, not dicts
        )
        if own_keys and llm.grounding_fallback is not None:
            if llm.grounding_fallback.provider in ADMIN_PROVIDERS:
                llm.grounding_fallback = None  # the admin's; not for this profile
        return self.model_copy(
            update={
                "profile": slug,
                "search": search,
                "web": web,
                "llm": llm,
                "own_keys": own_keys,
                "api_keys": read_secrets(folder / SECRETS_FILE) if own_keys else {},
                "cv_path": folder / "cvs" / "master.md",
                "ranking_config": folder / "ranking.yaml",
                "companies_config": folder / "companies.yaml",
            }
        )

    @property
    def secrets_file(self) -> Path | None:
        """This profile's secrets.env (None when it uses .env's keys)."""
        if not self.own_keys or self.profiles_dir is None:
            return None
        return self.profiles_dir / self.profile / SECRETS_FILE

    @property
    def profile_file(self) -> Path | None:
        """This profile's profile.yaml (None for DEFAULT_PROFILE)."""
        if self.profile == DEFAULT_PROFILE or self.profiles_dir is None:
            return None
        return self.profiles_dir / self.profile / PROFILE_FILE

    @property
    def news_source(self) -> str:
        source = self.companies.news_source
        if source == "auto":
            return "gdelt" if self.crawl.respect_robots else "google_news"
        return source

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobsearcher.db"


def load_env_file(path: Path) -> None:
    """Read KEY=value lines from a .env file into the environment, for runs outside
    Docker (where compose's env_file does this). Variables already set win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if sep and key and not key.startswith("#") and value:
            os.environ.setdefault(key, value)


def config_file_path(path: str | Path | None = None) -> Path:
    """The config.yaml in use: `path`, else $JOBSEARCHER_CONFIG, else ./config.yaml."""
    return Path(path or os.environ.get("JOBSEARCHER_CONFIG", "config.yaml"))


def load_config(path: str | Path | None = None) -> Config:
    path = config_file_path(path)
    load_env_file(path.resolve().parent / ".env")
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    config = Config.model_validate(raw or {})
    base = path.resolve().parent
    for field, env in (
        ("data_dir", "JOBSEARCHER_DATA_DIR"),
        ("ranking_config", "JOBSEARCHER_RANKING_CONFIG"),
        ("cv_path", "JOBSEARCHER_CV"),
        ("companies_config", "JOBSEARCHER_COMPANIES"),
        ("profiles_dir", "JOBSEARCHER_PROFILES"),
    ):
        value = Path(os.environ.get(env) or getattr(config, field) or "profiles")
        setattr(config, field, value if value.is_absolute() else base / value)
    workdir = config.chat.workdir or Path()
    config.chat.workdir = workdir if workdir.is_absolute() else (base / workdir).resolve()
    return config
