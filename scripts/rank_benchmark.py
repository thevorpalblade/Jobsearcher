"""Compare ranking models on a fixed set of jobs.

    python scripts/rank_benchmark.py build                 # pick the jobs (once)
    python scripts/rank_benchmark.py run kimi-k3 --provider moonshot --model kimi-k3 \
        --extra '{"thinking": {"type": "disabled"}}'
    python scripts/rank_benchmark.py compare [--reference glm-5.3-flash] [--jobs]

The set keeps a copy of each job ad, so it survives jobs closing. The CV and ranking.yaml
are not frozen: runs use today's, so compare runs made with the same ones. Results go to
data/benchmark/ranking/<label>.json, one entry per job, and a stopped run carries on where
it left off. Calls are recorded in the usage table like any other ranking call (Moonshot
spends the monthly budget).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jobsearcher.config import Config, ModelRole, Provider, load_config
from jobsearcher.llm import BudgetTracker, LLMError, make_llm
from jobsearcher.models import Job
from jobsearcher.ranking.config import load_ranking_config
from jobsearcher.ranking.ranker import (
    SYSTEM_PROMPT,
    JobAssessment,
    build_context,
    final_score,
    job_prompt,
)
from jobsearcher.store import Store

SPREAD = 30  # jobs spread evenly over the reference model's scores
TOP = 10  # plus the reference model's best, where getting it right matters most


def bench_dir(config: Config) -> Path:
    return Path(config.data_dir) / "benchmark" / "ranking"


def load_set(config: Config) -> list[Job]:
    path = bench_dir(config) / "jobs.json"
    if not path.exists():
        sys.exit("No benchmark set yet: run `build` first")
    return [Job.model_validate(j) for j in json.loads(path.read_text())]


def cmd_build(config: Config, args: argparse.Namespace) -> None:
    """The set: jobs the reference model ranked, spread over its scores, plus its top."""
    store = Store(config.db_path)
    rc = load_ranking_config(config.ranking_config)
    ranked: dict[str, JobAssessment] = {}
    for row in store.conn.execute("SELECT job_id, data FROM rankings"):
        data = json.loads(row["data"])
        if args.reference_model in data["model"]:
            ranked[row["job_id"]] = JobAssessment.model_validate(data["assessment"])
    ids = sorted(ranked, key=lambda j: final_score(ranked[j], rc))
    if len(ids) < SPREAD + TOP:
        sys.exit(f"Only {len(ids)} jobs ranked by {args.reference_model}")
    step = len(ids) // SPREAD
    chosen = ids[::step][:SPREAD]
    chosen += [j for j in reversed(ids) if j not in chosen][:TOP]
    jobs = [store.get_job(j) for j in chosen]
    out = bench_dir(config)
    out.mkdir(parents=True, exist_ok=True)
    (out / "jobs.json").write_text(
        json.dumps([j.model_dump(mode="json") for j in jobs if j], ensure_ascii=False, indent=1)
    )
    # The reference model's existing rankings are its run; no need to call it again.
    reference = {j: {"assessment": ranked[j].model_dump()} for j in chosen}
    (out / f"{args.reference_label}.json").write_text(json.dumps(reference, ensure_ascii=False))
    print(f"{len(jobs)} jobs; {args.reference_label} results taken from the database")


def cmd_run(config: Config, args: argparse.Namespace) -> None:
    jobs = load_set(config)
    rc = load_ranking_config(config.ranking_config)
    cv = Path(config.cv_path).read_text()
    config.llm.ranking = ModelRole(
        provider=Provider(args.provider),
        model=args.model,
        effort=args.effort,
        extra_body=json.loads(args.extra) if args.extra else {},
        enforce_schema=args.provider in ("moonshot", "nvidia", "ollama"),
        timeout_s=args.timeout,
        max_retries=1,
    )
    if args.rpm is not None:
        config.llm.moonshot_requests_per_minute = args.rpm
    store = Store(config.db_path)
    llm = make_llm(config, "ranking", BudgetTracker(store, config.llm))
    path = bench_dir(config) / f"{args.label}.json"
    results: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    context = build_context(cv, rc)
    for n, job in enumerate(jobs, 1):
        if job.id in results:
            continue
        start = time.monotonic()
        llm.check()  # stops at the monthly budget (pay-per-token providers)
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
        u = result.usage
        results[job.id] = {
            "assessment": assessment.model_dump(),
            "seconds": round(time.monotonic() - start, 1),
            "usage": {
                "model": u.model,
                "input": u.input_tokens,
                "output": u.output_tokens,
                "cache_read": u.cache_read_tokens,
                "cache_write": u.cache_write_tokens,
            },
            "cost_usd": llm.tracker.cost(u),
        }
        path.write_text(json.dumps(results, ensure_ascii=False))
        print(
            f"{n}/{len(jobs)} {final_score(assessment, rc):3d} {job.title[:60]} "
            f"({results[job.id]['seconds']} s)",
            flush=True,
        )
    print(f"{len(results)}/{len(jobs)} done -> {path}")


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for rank, i in enumerate(order):
        ranks[i] = rank
    return ranks


def cmd_compare(config: Config, args: argparse.Namespace) -> None:
    jobs = load_set(config)
    rc = load_ranking_config(config.ranking_config)
    runs: dict[str, dict[str, Any]] = {
        p.stem: json.loads(p.read_text())
        for p in sorted(bench_dir(config).glob("*.json"))
        if p.name != "jobs.json"
    }
    if args.reference not in runs:
        sys.exit(f"No run called {args.reference}; have: {', '.join(runs)}")

    def scores(label: str) -> dict[str, int]:
        return {
            j: final_score(JobAssessment.model_validate(r["assessment"]), rc)
            for j, r in runs[label].items()
        }

    ref = scores(args.reference)
    ref_top = set(sorted(ref, key=lambda j: -ref[j])[:TOP])
    print(
        f"Reference: {args.reference}. Spearman = rank agreement; top-{TOP} = how many of "
        f"the reference's best {TOP} are also in this model's best {TOP} (of the same jobs)."
    )
    columns = ("jobs", "mean", "pearson", "spearman", "|diff|", "top", "s/job", "out tok", "$")
    print(f"{'model':24}" + "".join(f"{c:>9}" for c in columns))
    for label, run in runs.items():
        mine = scores(label)
        common = [j for j in mine if j in ref]
        a, b = [ref[j] for j in common], [mine[j] for j in common]
        top = set(sorted(common, key=lambda j: -mine[j])[:TOP])
        secs = [r["seconds"] for r in run.values() if "seconds" in r]
        out = [r["usage"]["output"] for r in run.values() if "usage" in r]
        cost = sum(r.get("cost_usd", 0) for r in run.values())
        enough = len(common) > 2
        cells = [
            len(mine),
            round(statistics.mean(mine.values())),
            f"{statistics.correlation(a, b):.2f}" if enough else "-",
            f"{statistics.correlation(_ranks(a), _ranks(b)):.2f}" if enough else "-",
            f"{statistics.mean(abs(x - y) for x, y in zip(a, b, strict=True)):.1f}",
            len(top & ref_top),
            round(statistics.mean(secs)) if secs else "-",
            round(statistics.mean(out)) if out else "-",
            f"{cost:.3f}",
        ]
        print(f"{label:24}" + "".join(f"{c:>9}" for c in cells))
    if args.jobs:
        labels = list(runs)
        print("\n" + " ".join(f"{label[:10]:>10}" for label in labels) + "  job")
        table = {label: scores(label) for label in labels}
        for job in sorted(jobs, key=lambda j: -ref.get(j.id, 0)):
            cells = " ".join(f"{table[label].get(job.id, '-'):>10}" for label in labels)
            print(f"{cells}  {job.title[:60]} ({job.company or ''})"[:200])


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--reference-model", default="glm-5.3-flash")
    b.add_argument("--reference-label", default="glm-5.3-flash")
    r = sub.add_parser("run")
    r.add_argument("label")
    r.add_argument("--provider", required=True, choices=[p.value for p in Provider])
    r.add_argument("--model", required=True)
    r.add_argument("--effort")
    r.add_argument("--extra", help="extra request fields as JSON (OpenAI-compatible providers)")
    r.add_argument("--timeout", type=float, default=600)
    r.add_argument("--rpm", type=int, help="Moonshot requests per minute")
    c = sub.add_parser("compare")
    c.add_argument("--reference", default="glm-5.3-flash")
    c.add_argument("--jobs", action="store_true", help="also list every job's scores")
    args = parser.parse_args()
    config = load_config(args.config)
    {"build": cmd_build, "run": cmd_run, "compare": cmd_compare}[args.command](config, args)


if __name__ == "__main__":
    main()
