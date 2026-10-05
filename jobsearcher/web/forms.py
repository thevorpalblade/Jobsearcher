"""Turn the structured settings forms (ranking, companies) into data for
`settings.merge_yaml`, which writes it into the YAML file with comments kept.

Repeated blocks (roles, companies) are named `<prefix>-<id>-<field>`; ids are opaque
(new blocks get client-made ones) and the order on the page is the order in the file.
An emptied optional field becomes `settings.DELETE`, so its key leaves the file.
"""

from __future__ import annotations

from typing import Any

from starlette.datastructures import FormData

from jobsearcher.ranking.config import RankingConfig
from jobsearcher.settings import DELETE


class FormErrors(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def lines(value: str | None) -> list[str]:
    """One item per line, blanks dropped (textareas for aliases, likes, filters)."""
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def commas(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def groups(form: FormData, prefix: str) -> list[dict[str, str]]:
    """The repeated blocks named `<prefix>-<id>-<field>`, in page order. For a field
    sent twice (a hidden default before a checkbox) the last value wins."""
    order: list[str] = []
    blocks: dict[str, dict[str, str]] = {}
    for key, value in form.multi_items():
        parts = key.split("-", 2)
        if len(parts) != 3 or parts[0] != prefix or not isinstance(value, str):
            continue
        _, block_id, field = parts
        if block_id not in blocks:
            order.append(block_id)
            blocks[block_id] = {}
        blocks[block_id][field] = value
    return [blocks[i] for i in order]


def _field(form: FormData, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def _number(form: FormData, name: str, label: str, errors: list[str], kind: type = float) -> Any:
    raw = _field(form, name)
    try:
        return kind(raw)
    except ValueError:
        errors.append(f"{label}: “{raw}” isn't a {'whole ' if kind is int else ''}number")
        return None


def _list_or_empty(values: list[str], old_section: dict, key: str) -> Any:
    """An emptied list: keep `key: []` if the file had the key, else leave it out."""
    if values:
        return values
    return [] if key in old_section else DELETE


def _text_or_empty(value: str, old_section: dict, key: str) -> Any:
    if value:
        return value
    return "" if key in old_section else DELETE


def ranking_data(form: FormData, old: dict[str, Any]) -> dict[str, Any]:
    """ranking.yaml values from the ranking form. `old` is the file's current data."""
    errors: list[str] = []
    roles = []
    for block in groups(form, "role"):
        name = block.get("name", "").strip()
        filters = {k: lines(block.get(k)) for k in ("aliases", "exclude", "except", "include")}
        if not name:
            if any(filters.values()):
                errors.append("A target role has aliases or filters but no name")
            continue  # an untouched new block
        roles.append(
            {
                "name": name,
                "aliases": filters["aliases"] or DELETE,
                "exclude_occupations": filters["exclude"] or DELETE,
                "except_occupations": filters["except"] or DELETE,
                "include_occupations": filters["include"] or DELETE,
            }
        )
    names = [r["name"].casefold() for r in roles]
    if len(set(names)) != len(names):
        errors.append("Two target roles have the same name")

    prefs_old = old.get("preferences") or {}
    preferences = {
        key: _text_or_empty(_field(form, f"pref-{key}"), prefs_old, key)
        for key in ("situation", "seniority", "languages")
    }
    preferences |= {
        key: _list_or_empty(lines(form.get(f"pref-{key}")), prefs_old, key)  # type: ignore[arg-type]
        for key in ("likes", "dislikes", "dealbreakers")
    }

    sections: dict[str, Any] = {
        "weights": {
            "fit": _number(form, "weights-fit", "Fit weight", errors),
            "success": _number(form, "weights-success", "Success weight", errors),
        },
        "adjustments": {
            key: _number(form, f"adj-{key}", label, errors, int)
            for key, label in (
                ("english_ad", "English ad"),
                ("swedish_required", "Swedish required"),
                ("swedish_merit", "Swedish as a merit"),
            )
        },
        "prefilter": {
            "require_role_match": _field(form, "prefilter-require_role_match") == "1",
            "max_llm_calls_per_run": _number(
                form, "prefilter-max_llm_calls_per_run", "Jobs ranked per run", errors, int
            ),
        },
        "drafting": {
            "min_score": _number(form, "drafting-min_score", "Drafting minimum score", errors, int),
            "max_drafts_per_day": _number(
                form, "drafting-max_drafts_per_day", "Drafts per day", errors, int
            ),
        },
    }
    if errors:
        raise FormErrors(errors)

    defaults = RankingConfig().model_dump()
    data: dict[str, Any] = {"target_roles": roles, "preferences": preferences}
    for key, value in sections.items():
        # Don't add a section the file doesn't have just to restate the defaults.
        if key in old or value != defaults[key]:
            data[key] = value
    return data


def companies_data(form: FormData) -> dict[str, Any]:
    """companies.yaml values from the companies form."""
    errors: list[str] = []
    companies = []
    for block in groups(form, "co"):
        name = block.get("name", "").strip()
        values = {k: block.get(k, "").strip() for k in block}
        if not name:
            if any(v for k, v in values.items() if k not in ("enabled", "ats_type")):
                errors.append("A company row has details but no name")
            continue
        ats_type, ats_ref = values.get("ats_type", ""), values.get("ats_ref", "")
        if bool(ats_type) != bool(ats_ref):
            errors.append(f"{name}: an ATS override needs both a type and a reference")
        companies.append(
            {
                "name": name,
                "website": values.get("website") or DELETE,
                "careers_url": values.get("careers_url") or DELETE,
                "location": values.get("location") or DELETE,
                "org_nr": values.get("org_nr") or DELETE,
                "tags": commas(values.get("tags")) or DELETE,
                "source": values.get("source") or DELETE,
                "news_query": values.get("news_query") or DELETE,
                "ats": {"type": ats_type, "ref": ats_ref} if ats_type and ats_ref else DELETE,
                # Enabled is the default, so only a disabled company says so.
                "enabled": DELETE if values.get("enabled") == "1" else False,
            }
        )
    seen: set[str] = set()
    for company in companies:
        key = company["name"].casefold()
        if key in seen:
            errors.append(f"Duplicate company: {company['name']}")
        seen.add(key)
    if errors:
        raise FormErrors(errors)
    return {"companies": companies}


def without_deletes(value: Any) -> Any:
    """The data as the file will contain it (for validating before writing)."""
    if isinstance(value, dict):
        return {k: without_deletes(v) for k, v in value.items() if v is not DELETE}
    if isinstance(value, list):
        return [without_deletes(v) for v in value]
    return value
