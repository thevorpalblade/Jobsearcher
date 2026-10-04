"""Edit the YAML config files from the web UI: validate, explain the effect, save.

Files are edited as text, so their comments survive. Saving validates with the same
models the pipeline loads, backs the old file up to `data/backups/`, and writes in
place (Docker bind-mounts single files, which can't be replaced by a rename).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from jobsearcher.companies.config import CompaniesConfig
from jobsearcher.config import Config
from jobsearcher.ranking.config import RankingConfig

PACKAGE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ConfigFile:
    key: str
    title: str
    description: str
    model: type[BaseModel]
    example: str  # file name of the committed example, next to the package
    path: Callable[[Config, Path], Path]  # (config, config.yaml path) -> this file


FILES: dict[str, ConfigFile] = {
    f.key: f
    for f in (
        ConfigFile(
            "config",
            "config.yaml",
            "Search locations and keywords, sources, LLM providers and models, budget, "
            "schedule, company crawling. API keys stay in .env and aren't editable here.",
            Config,
            "config.example.yaml",
            lambda config, config_path: config_path,
        ),
        ConfigFile(
            "ranking",
            "ranking.yaml",
            "Target roles and aliases, occupation filters, preferences, score weights and "
            "adjustments, per-run limits, drafting thresholds.",
            RankingConfig,
            "ranking.example.yaml",
            lambda config, config_path: config.ranking_config,
        ),
        ConfigFile(
            "companies",
            "companies.yaml",
            "Target companies whose careers sites are crawled and whose news is watched.",
            CompaniesConfig,
            "companies.example.yaml",
            lambda config, config_path: config.companies_config,
        ),
    )
}


def example_text(file: ConfigFile) -> str:
    # Installed wheels carry the examples in jobsearcher/examples/; a source checkout
    # has them in the repo root.
    for path in (PACKAGE / "examples" / file.example, PACKAGE.parent / file.example):
        if path.is_file():
            return path.read_text()
    return ""


def parse(file: ConfigFile, text: str) -> tuple[BaseModel | None, list[str]]:
    """The validated model, or a list of readable errors."""
    try:
        raw: Any = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        return None, [f"YAML syntax error at {where}{problem}"]
    if not isinstance(raw, dict):
        return None, ["The file must be a YAML mapping (key: value lines) at the top level."]
    try:
        model = file.model.model_validate(raw)
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(p) for p in err['loc']) or '(top level)'}: {err['msg']}"
            for err in exc.errors()
        ]
    if isinstance(model, CompaniesConfig):
        seen: set[str] = set()
        for company in model.companies:
            if company.slug in seen:
                return None, [f"Duplicate company: {company.name}"]
            seen.add(company.slug)
    return model, []


def effects(file: ConfigFile, old_text: str, new: BaseModel) -> list[str]:
    """What saving this change will cause, in plain words (best effort)."""
    old, _ = parse(file, old_text)
    notes: list[str] = []
    if isinstance(new, RankingConfig) and isinstance(old, RankingConfig):
        if new.fingerprint() != old.fingerprint():
            notes.append(
                "Target roles or preferences changed: every open job will be re-ranked "
                "on the next ranking run."
            )
        if new.filter_fingerprint() != old.filter_fingerprint():
            notes.append("The prefilter changes which jobs are sent for ranking (no re-rank).")
        if (new.weights, new.adjustments) != (old.weights, old.adjustments):
            notes.append("Weights or adjustments changed: scores update immediately, free.")
    if isinstance(new, Config) and isinstance(old, Config):
        if new.llm.ranking.model != old.llm.ranking.model:
            notes.append("The ranking model changed: every open job will be re-ranked.")
        if (new.data_dir, new.web) != (old.data_dir, old.web):
            notes.append("data_dir or web settings changed: restart `jobsearcher web`.")
        if new.search != old.search or new.sources != old.sources:
            notes.append("Search settings changed: they apply from the next search.")
    if isinstance(new, CompaniesConfig) and isinstance(old, CompaniesConfig):
        added = {c.slug for c in new.companies} - {c.slug for c in old.companies}
        if added:
            notes.append(
                f"{len(added)} new compan{'y' if len(added) == 1 else 'ies'}: "
                "their careers sites are detected on the next search."
            )
    return notes


def save(path: Path, text: str, backup_dir: Path) -> Path | None:
    """Back the current file up, then write `text` in place. Returns the backup."""
    backup = None
    if path.is_file():
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_dir / f"{path.stem}-{stamp}{path.suffix}"
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    # In place, not write-and-rename: a Docker bind-mounted file can't be replaced.
    with path.open("w") as f:
        f.write(text)
    return backup
