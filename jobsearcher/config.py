"""Configuration: `config.yaml` for settings, environment variables for secrets."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any

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


class SourcesConfig(BaseModel):
    platsbanken: bool = True
    jobtech_links: bool = True


class Provider(StrEnum):
    MOONSHOT = "moonshot"  # Kimi models, OpenAI-compatible API
    ANTHROPIC = "anthropic"  # Claude models, pay-per-token API key
    NVIDIA = "nvidia"  # models on NVIDIA's serverless endpoints (e.g. GLM), OpenAI-compatible API
    CLAUDE_CODE = "claude_code"  # Claude via the Claude Code CLI on a Claude subscription


class ModelRole(BaseModel):
    provider: Provider
    model: str
    # anthropic / claude_code: effort level (low | medium | high | xhigh | max).
    # Leave unset for models that don't support it (e.g. claude-haiku-4-5).
    effort: str | None = None
    max_tokens: int = 16000
    # claude_code: seconds before a CLI call is abandoned.
    timeout_s: float = 600
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
    "claude-haiku-4-5": ModelPrice(input=1.00, output=5.00, cache_read=0.10, cache_write=1.25),
    "claude-sonnet-5-5": ModelPrice(input=2.00, output=10.00, cache_read=0.20, cache_write=2.50),
    "claude-opus-5-5": ModelPrice(input=4.00, output=20.00, cache_read=0.20, cache_write=5.00),
    # Possible targets of Anthropic's server-side refusal fallback.
    "claude-opus-5": ModelPrice(input=5.00, output=25.00, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-8": ModelPrice(input=5.00, output=25.00, cache_read=0.50, cache_write=6.25),
}


class LLMConfig(BaseModel):
    ranking: ModelRole = Field(
        default_factory=lambda: ModelRole(provider=Provider.MOONSHOT, model="kimi-k2.6")
    )
    drafting: ModelRole = Field(
        default_factory=lambda: ModelRole(provider=Provider.MOONSHOT, model="kimi-k3")
    )
    moonshot_base_url: str = "https://api.moonshot.ai/v1"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
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


class WebConfig(BaseModel):
    # `jobsearcher web` defaults; the Docker service passes --host 0.0.0.0.
    host: str = "127.0.0.1"
    port: int = 8080


class Config(BaseModel):
    search: SearchConfig = Field(default_factory=SearchConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    data_dir: Path = Path("data")
    # Relative paths are resolved against the directory containing config.yaml.
    ranking_config: Path = Path("ranking.yaml")
    cv_path: Path = Path("cvs/master.md")

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


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("JOBSEARCHER_CONFIG", "config.yaml"))
    load_env_file(path.resolve().parent / ".env")
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    config = Config.model_validate(raw or {})
    base = path.resolve().parent
    for field, env in (
        ("data_dir", "JOBSEARCHER_DATA_DIR"),
        ("ranking_config", "JOBSEARCHER_RANKING_CONFIG"),
        ("cv_path", "JOBSEARCHER_CV"),
    ):
        value = Path(os.environ.get(env) or getattr(config, field))
        setattr(config, field, value if value.is_absolute() else base / value)
    return config
