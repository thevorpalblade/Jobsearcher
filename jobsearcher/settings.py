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
from jobsearcher.config import Config, ProfileSettings
from jobsearcher.ranking.config import RankingConfig

PACKAGE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ConfigFile:
    key: str
    title: str
    description: str
    model: type[BaseModel]
    example: str  # file name of the committed example, next to the package
    # (config, config.yaml path) -> this file; None when the setup has no such file
    path: Callable[[Config, Path], Path | None]


FILES: dict[str, ConfigFile] = {
    f.key: f
    for f in (
        ConfigFile(
            "config",
            "config.yaml",
            "Sources, LLM providers and models, budget, schedule, company crawling, and "
            "(without profiles) search locations and keywords. API keys stay in .env and "
            "aren't editable here.",
            Config,
            "config.example.yaml",
            lambda config, config_path: config_path,
        ),
        ConfigFile(
            "profile",
            "profile.yaml",
            "This candidate's name and search: keywords, excluded words, locations.",
            ProfileSettings,
            "profile.example.yaml",
            lambda config, config_path: config.profile_file,
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


def available(config: Config, config_path: Path) -> list[tuple[ConfigFile, Path]]:
    """The files this setup has, with their paths."""
    out = []
    for file in FILES.values():
        path = file.path(config, config_path)
        if path is not None:
            out.append((file, path))
    return out


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
    if isinstance(new, ProfileSettings) and isinstance(old, ProfileSettings):
        if new.search != old.search:
            notes.append(
                "Search settings changed: new keywords and places apply from the next "
                "search; ranking picks jobs with the new filters on its next run."
            )
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


# --- structured editing: merge form values into the YAML, keeping comments -----------

# A field set to DELETE in the form data is removed from the file (e.g. emptied).
DELETE = None


def _yaml() -> Any:
    from ruamel.yaml import YAML

    y = YAML()  # round-trip: keeps comments, key order, quoting and flow style
    y.preserve_quotes = True
    y.width = 4096  # never re-wrap lines (companies.yaml has one long line per company)
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _plain(node: Any) -> Any:
    """A ruamel node as plain Python values, for comparisons."""
    if isinstance(node, dict):
        return {k: _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    if isinstance(node, str):
        return str(node)
    return node


def _new_node(value: Any, flow: bool = False) -> Any:
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    if isinstance(value, dict):
        node = CommentedMap((k, _new_node(v)) for k, v in value.items() if v is not DELETE)
        if flow:
            node.fa.set_flow_style()
        return node
    if isinstance(value, list):
        node = CommentedSeq(_new_node(v) for v in value)
        if all(not isinstance(v, dict | list) for v in value):
            node.fa.set_flow_style()  # short scalar lists read best as [a, b, c]
        return node
    return value


def _merge(old: Any, new: Any, key: str | None = None) -> Any:
    """`new` expressed as an edit of the ruamel node `old`: unchanged parts keep their
    node (and so their comments and formatting)."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.scalarstring import ScalarString

    if _plain(old) == new:
        return old
    if isinstance(new, dict) and isinstance(old, CommentedMap):
        for k, v in new.items():
            if v is DELETE:
                old.pop(k, None)
            elif k in old:
                old[k] = _merge(old[k], v, k)
            else:
                old[k] = _new_node(v)
        return old
    if isinstance(new, list) and isinstance(old, CommentedSeq):
        # Match mapping items by name, so editing or removing one company or role
        # leaves the others' nodes (and comments) alone.
        by_name = {
            str(item.get("name")): item
            for item in old
            if isinstance(item, CommentedMap) and item.get("name") is not None
        }
        flow_items = any(isinstance(i, CommentedMap) and i.fa.flow_style() for i in old)
        matches = []
        for i, value in enumerate(new):
            match = by_name.get(str(value.get("name"))) if isinstance(value, dict) else None
            if match is None and not isinstance(value, dict) and i < len(old):
                match = old[i]
            matches.append(match)
        kept = [id(m) for m in matches if m is not None]
        if kept == [id(item) for item in old if id(item) in set(kept)]:
            # Same order: delete and append in place. ruamel keeps comments by item
            # position and shifts them on `del`, so other items keep theirs.
            for i in reversed(range(len(old))):
                if id(old[i]) not in set(kept):
                    del old[i]
            position = {id(item): i for i, item in enumerate(old)}
            for match, value in zip(matches, new, strict=True):
                if match is None:
                    old.append(_new_node(value, flow_items))
                else:
                    i = position[id(match)]
                    old[i] = _merge(match, value)
            return old
        old[:] = [  # reordered: rebuild (comments between items may move)
            _merge(m, v) if m is not None else _new_node(v, flow_items)
            for m, v in zip(matches, new, strict=True)
        ]
        return old
    if isinstance(old, ScalarString) and isinstance(new, str):
        return type(old)(new)  # keep folded (>-) or quoted style
    return _new_node(new)


def merge_yaml(text: str, data: dict[str, Any]) -> str:
    """`text` with the values in `data` applied (DELETE removes a key), comments kept."""
    import io

    from ruamel.yaml.comments import CommentedMap

    y = _yaml()
    doc = y.load(text) if text.strip() else None
    if not isinstance(doc, CommentedMap):
        doc = CommentedMap()
    _merge(doc, data)
    out = io.StringIO()
    y.dump(doc, out)
    return out.getvalue()
