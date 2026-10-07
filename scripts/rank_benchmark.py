"""Compare ranking models on a fixed set of jobs.

    python scripts/rank_benchmark.py build                 # pick the jobs (once)
    python scripts/rank_benchmark.py run kimi-k3 --provider moonshot --model kimi-k3 \
        --extra '{"thinking": {"type": "disabled"}}'
    python scripts/rank_benchmark.py compare [--reference opus-5.5] [--jobs]

The set (job ads) and each model's scores are committed in benchmarks/ranking/, so a new
model can be ranked against the earlier ones. Only scores go into git: the rationale and
requirement lists describe the CV, so the full assessments stay in the gitignored
data/benchmark/ranking/. The committed ads have the recruiters' names, emails and phone
numbers removed; runs use the full ads in data/benchmark/ranking/jobs.json when present, so
they see exactly what the pipeline sees. The CV and ranking.yaml are not frozen, so each run
records their hashes and `compare` warns when runs were made with different ones.

A stopped run carries on where it left off. Calls are recorded in the usage table like
any other ranking call (Moonshot spends the monthly budget).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobsearcher.config import Config, ModelRole, Provider, load_config
from jobsearcher.cvs import ranking_cv
from jobsearcher.llm import BudgetTracker, LLMError, make_llm
from jobsearcher.models import Job
from jobsearcher.ranking.config import RankingConfig, load_ranking_config
from jobsearcher.ranking.ranker import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    JobAssessment,
    build_context,
    final_score,
    input_hash,
    job_prompt,
)
from jobsearcher.store import Store

BENCH = Path(__file__).resolve().parent.parent / "benchmarks" / "ranking"
SPREAD = 30  # jobs spread evenly over the reference model's scores
TOP = 10  # plus the reference model's best, where getting it right matters most
# The parts of an assessment that final_score uses; safe to commit (no CV details).
SCORE_FIELDS = ("fit_score", "success_score", "matched_role", "language", "swedish")
# What `compare` checks runs agree on before comparing them.
INPUTS = ("prompt_version", "cv_sha", "ranking_sha")


def private_dir(config: Config) -> Path:
    return Path(config.data_dir) / "benchmark" / "ranking"


def cv_text(config: Config) -> str:
    cv = ranking_cv(Path(config.cv_path))
    if cv is None:
        sys.exit(f"Master CV not found at {config.cv_path}")
    return cv


def inputs(config: Config, rc: RankingConfig) -> dict[str, str]:
    return {
        "prompt_version": PROMPT_VERSION,
        "cv_sha": hashlib.sha256(cv_text(config).encode()).hexdigest()[:12],
        "ranking_sha": rc.fingerprint()[:12],
    }


def load_set(config: Config) -> list[Job]:
    """The full ads if this machine has them, else the committed copy without contacts."""
    for path in (private_dir(config) / "jobs.json", BENCH / "jobs.json"):
        if path.exists():
            return [Job.model_validate(j) for j in json.loads(path.read_text())]
    sys.exit("No benchmark set yet: run `build` first")


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
# Swedish numbers (+46 or a leading 0), with the usual spaces, dashes and brackets.
PHONE = re.compile(r"(\+46|\b0)[\d\s()/-]{6,}\d")


def persons(name: str) -> list[tuple[str, ...]]:
    """The people in a contact name, as word lists: "Mikael Listh, Seko" and
    "Vision, Annika Engberg" (a union rep) name one person each."""
    segments = [tuple(seg.split()) for seg in name.split(",")]
    people = [seg for seg in segments if len(seg) >= 2]
    if not people and len(segments) == 1 and len(segments[0]) == 1 and "@" not in name:
        people = segments  # a first name on its own
    return [p for p in people if len(p[0]) > 2]


def strip_contacts(job: Job, names: set[str]) -> Job:
    """The ad without the people in it: structured contacts, application email, and their
    names, emails and phone numbers in the text. `names` adds people the models found."""
    people = {p for n in names | {c.name for c in job.contacts if c.name} for p in persons(n)}
    text = job.description
    # Full names, then first names alone ("kontakta Anna"); surnames alone are skipped,
    # since some are ordinary words (Fast, Strid).
    patterns = {r"\s+".join(map(re.escape, p)) for p in people} | {re.escape(p[0]) for p in people}
    for pattern in sorted(patterns, key=len, reverse=True):
        text = re.sub(rf"\b{pattern}\b", "[name]", text)
    text = PHONE.sub("[phone]", EMAIL.sub("[email]", text))
    return job.model_copy(update={"description": text, "contacts": [], "apply_email": None})


def write_set(config: Config, jobs: list[Job]) -> None:
    """Full ads to data/ (what runs use), and the committed copy without contacts."""
    names: set[str] = set()
    for path in private_dir(config).glob("*.json"):
        if path.name != "jobs.json":
            for assessment in json.loads(path.read_text()).values():
                names |= {p["name"] for p in assessment.get("contact_persons", []) if p["name"]}
    for path, out in (
        (private_dir(config) / "jobs.json", jobs),
        (BENCH / "jobs.json", [strip_contacts(j, names) for j in jobs]),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [j.model_dump(mode="json") for j in out]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def load_run(label: str) -> dict[str, Any]:
    path = BENCH / "runs" / f"{label}.json"
    return json.loads(path.read_text()) if path.exists() else {"meta": {}, "results": {}}


def save_run(label: str, run: dict[str, Any]) -> None:
    path = BENCH / "runs" / f"{label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run, ensure_ascii=False, indent=1) + "\n")


def save_private(config: Config, label: str, job_id: str, assessment: JobAssessment) -> None:
    path = private_dir(config) / f"{label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    data[job_id] = assessment.model_dump()
    path.write_text(json.dumps(data, ensure_ascii=False))


def scores_only(assessment: JobAssessment) -> dict[str, Any]:
    return {k: getattr(assessment, k) for k in SCORE_FIELDS}


def score(result: dict[str, Any], rc: RankingConfig) -> int:
    assessment = JobAssessment.model_validate(
        {k: result[k] for k in SCORE_FIELDS}
        | {
            "matched_requirements": [],
            "missing_requirements": [],
            "red_flags": [],
            "rationale": "",
            "contact_persons": [],
        }
    )
    return final_score(assessment, rc)


def cmd_build(config: Config, args: argparse.Namespace) -> None:
    """The set: jobs the reference model ranked, spread over its scores, plus its top.
    Its existing rankings made with today's CV and ranking.yaml become its run."""
    if (BENCH / "jobs.json").exists() and not args.force:
        sys.exit("The set exists; --force replaces it (and makes earlier runs incomparable)")
    store = Store(config.db_path)
    rc = load_ranking_config(config.ranking_config)
    latest = reference_rankings(config, args.reference_model)
    ids = sorted(latest, key=lambda j: final_score(latest[j][1], rc))
    if len(ids) < SPREAD + TOP:
        sys.exit(f"Only {len(ids)} jobs ranked by {args.reference_model}")
    chosen = ids[:: len(ids) // SPREAD][:SPREAD]
    chosen += [j for j in reversed(ids) if j not in chosen][:TOP]
    jobs = [job for j in chosen if (job := store.get_job(j))]
    write_set(config, jobs)
    print(f"{len(jobs)} jobs")
    cmd_import(config, argparse.Namespace(model=args.reference_model, label=args.reference_label))


def reference_rankings(config: Config, model: str) -> dict[str, tuple[bool, JobAssessment]]:
    """The newest ranking of each job by `model` in the database, and whether it was made
    with today's CV, ranking.yaml and prompt."""
    store = Store(config.db_path)
    rc = load_ranking_config(config.ranking_config)
    cv = cv_text(config)
    found: dict[str, tuple[bool, JobAssessment]] = {}
    rows = store.conn.execute("SELECT job_id, input_hash, data FROM rankings ORDER BY created_at")
    for row in rows:
        data = json.loads(row["data"])
        if model not in data["model"]:
            continue
        job = store.get_job(row["job_id"])
        current = job is not None and row["input_hash"] == input_hash(job, cv, rc, data["model"])
        if current or row["job_id"] not in found or not found[row["job_id"]][0]:
            found[row["job_id"]] = (current, JobAssessment.model_validate(data["assessment"]))
    return found


def cmd_import(config: Config, args: argparse.Namespace) -> None:
    """A model's run from the rankings the pipeline already made, where they were made
    with today's inputs (free: no model calls)."""
    rc = load_ranking_config(config.ranking_config)
    found = reference_rankings(config, args.model)
    run = load_run(args.label)
    run["meta"] = run["meta"] | {"model": args.model, "source": "pipeline rankings"}
    run["meta"] |= inputs(config, rc)
    jobs = load_set(config)
    for job in jobs:
        current, assessment = found.get(job.id, (False, None))
        if current and assessment is not None:
            run["results"][job.id] = scores_only(assessment)
            save_private(config, args.label, job.id, assessment)
    save_run(args.label, run)
    print(
        f"{args.label}: {len(run['results'])} of {len(jobs)} jobs have a ranking made with "
        "today's CV and ranking.yaml; `run` the model to fill in the rest"
    )


def cmd_run(config: Config, args: argparse.Namespace) -> None:
    jobs = load_set(config)
    rc = load_ranking_config(config.ranking_config)
    cv = cv_text(config)
    extra = json.loads(args.extra) if args.extra else {}
    config.llm.ranking = ModelRole(
        provider=Provider(args.provider),
        model=args.model,
        effort=args.effort,
        extra_body=extra,
        enforce_schema=args.provider in ("moonshot", "nvidia", "ollama"),
        timeout_s=args.timeout,
        max_retries=1,
    )
    if args.rpm is not None:
        config.llm.moonshot_requests_per_minute = args.rpm
    if args.no_thinking:
        # Claude Code reads this; Haiku otherwise thinks for 3-9k tokens per job (60-90 s).
        os.environ["MAX_THINKING_TOKENS"] = "0"
    store = Store(config.db_path)
    llm = make_llm(config, "ranking", BudgetTracker(store, config.llm))
    run = load_run(args.label)
    now = inputs(config, rc)
    if run["results"] and any(run["meta"].get(k) != v for k, v in now.items()):
        sys.exit(
            f"{args.label} was run with another CV, ranking.yaml or prompt; "
            "use a new label (or delete the run)"
        )
    run["meta"] |= {
        "provider": args.provider,
        "model": args.model,
        "effort": args.effort,
        "extra_body": extra,
        "thinking": not args.no_thinking,
        "date": datetime.now(UTC).date().isoformat(),
    } | now
    context = build_context(cv, rc)
    for n, job in enumerate(jobs, 1):
        if job.id in run["results"]:
            continue
        llm.check()  # stops at the monthly budget (pay-per-token providers)
        start = time.monotonic()
        try:
            result = llm.call(
                system=SYSTEM_PROMPT, context=context, prompt=job_prompt(job), schema=JobAssessment
            )
        except LLMError as exc:
            if exc.usage is not None:
                llm.record(exc.usage)
            print(f"{n}/{len(jobs)} FAILED {job.title[:50]}: {str(exc)[:200]}", flush=True)
            continue
        llm.record(result.usage)
        assessment: JobAssessment = result.parsed  # type: ignore[assignment]
        run["results"][job.id] = scores_only(assessment) | {
            "seconds": round(time.monotonic() - start, 1),
            "output_tokens": result.usage.output_tokens,
            "cost_usd": round(llm.tracker.cost(result.usage), 5),
        }
        save_run(args.label, run)
        save_private(config, args.label, job.id, assessment)
        print(
            f"{n}/{len(jobs)} {final_score(assessment, rc):3d} {job.title[:60]} "
            f"({run['results'][job.id]['seconds']} s)",
            flush=True,
        )
    print(f"{len(run['results'])}/{len(jobs)} done")


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for rank, i in enumerate(order):
        ranks[i] = rank
    return ranks


def cmd_compare(config: Config, args: argparse.Namespace) -> None:
    jobs = load_set(config)
    rc = load_ranking_config(config.ranking_config)
    runs = {p.stem: json.loads(p.read_text()) for p in sorted((BENCH / "runs").glob("*.json"))}
    if args.reference not in runs:
        sys.exit(f"No run called {args.reference}; have: {', '.join(runs)}")
    scores = {
        label: {j: score(r, rc) for j, r in run["results"].items()} for label, run in runs.items()
    }
    ref = scores[args.reference]
    ref_meta = runs[args.reference]["meta"]
    for label, run in runs.items():
        differ = [k for k in INPUTS if run["meta"].get(k) != ref_meta.get(k)]
        if differ:
            print(f"Warning: {label} differs from {args.reference} in {', '.join(differ)}")
    print(
        f"Reference: {args.reference}. Compared on the jobs both ranked. spearman = rank "
        f"agreement; top = how many of the reference's best {TOP} are also in this model's "
        f"best {TOP}."
    )
    columns = ("jobs", "mean", "pearson", "spearman", "|diff|", "top", "s/job", "out tok", "$")
    print(f"{'model':24}" + "".join(f"{c:>9}" for c in columns))
    for label, run in runs.items():
        mine = scores[label]
        common = [j for j in mine if j in ref]
        a, b = [ref[j] for j in common], [mine[j] for j in common]
        ref_top = set(sorted(common, key=lambda j: -ref[j])[:TOP])
        top = set(sorted(common, key=lambda j: -mine[j])[:TOP])
        results = run["results"].values()
        secs = [r["seconds"] for r in results if "seconds" in r]
        out = [r["output_tokens"] for r in results if "output_tokens" in r]
        enough = len(common) > 2
        cells = [
            len(mine),
            round(statistics.mean(mine.values())) if mine else "-",
            f"{statistics.correlation(a, b):.2f}" if enough else "-",
            f"{statistics.correlation(_ranks(a), _ranks(b)):.2f}" if enough else "-",
            f"{statistics.mean(abs(x - y) for x, y in zip(a, b, strict=True)):.1f}"
            if common
            else "-",
            len(top & ref_top),
            round(statistics.mean(secs)) if secs else "-",
            round(statistics.mean(out)) if out else "-",
            f"{sum(r.get('cost_usd', 0) for r in results):.3f}",
        ]
        print(f"{label:24}" + "".join(f"{c:>9}" for c in cells))
    if args.jobs:
        labels = list(runs)
        print("\n" + " ".join(f"{label[:10]:>10}" for label in labels) + "  job")
        for job in sorted(jobs, key=lambda j: -ref.get(j.id, -1)):
            cells = " ".join(f"{scores[label].get(job.id, '-'):>10}" for label in labels)
            print(f"{cells}  {job.title[:60]} ({job.company or ''})"[:200])


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="pick the job set from a model's existing rankings")
    b.add_argument("--reference-model", default="glm-5.3-flash")
    b.add_argument("--reference-label", default="glm-5.3-flash")
    b.add_argument("--force", action="store_true")
    i = sub.add_parser("import", help="a run from the pipeline's current rankings (free)")
    i.add_argument("label")
    i.add_argument("--model", required=True, help="matches the ranking's model name")
    r = sub.add_parser("run", help="rank the set with a model")
    r.add_argument("label")
    r.add_argument("--provider", required=True, choices=[p.value for p in Provider])
    r.add_argument("--model", required=True)
    r.add_argument("--effort")
    r.add_argument("--extra", help="extra request fields as JSON (OpenAI-compatible providers)")
    r.add_argument("--timeout", type=float, default=600)
    r.add_argument("--rpm", type=int, help="Moonshot requests per minute")
    r.add_argument("--no-thinking", action="store_true", help="claude_code: thinking off")
    c = sub.add_parser("compare")
    c.add_argument("--reference", default="opus-5.5")
    c.add_argument("--jobs", action="store_true", help="also list every job's scores")
    args = parser.parse_args()
    config = load_config(args.config)
    commands = {"build": cmd_build, "import": cmd_import, "run": cmd_run, "compare": cmd_compare}
    commands[args.command](config, args)


if __name__ == "__main__":
    main()
