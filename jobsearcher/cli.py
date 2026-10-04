"""Command-line entry point: `jobsearcher <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from jobsearcher.config import Config, config_file_path, load_config
from jobsearcher.models import JobStatus
from jobsearcher.pipeline import run_search, search_keywords
from jobsearcher.store import Store

log = logging.getLogger("jobsearcher")


def cmd_search(config: Config, args: argparse.Namespace) -> int:
    keywords = search_keywords(config)
    if not keywords:
        print(
            "No keywords: set search.keywords in config.yaml or target_roles in ranking.yaml",
            file=sys.stderr,
        )
        return 2
    store = Store(config.db_path)
    report = run_search(config, store, keywords=keywords)
    print(
        f"search: {len(keywords)} keywords, fetched={report.fetched} "
        f"filtered_out={report.filtered_out} new={report.new} updated={report.updated} "
        f"expired={report.expired} open_total={store.count_jobs(JobStatus.OPEN)}"
    )
    if report.failed_sources:
        print(f"failed sources: {', '.join(report.failed_sources)}", file=sys.stderr)
        return 1
    return 0


def cmd_rank(config: Config, args: argparse.Namespace) -> int:
    from jobsearcher.llm import BudgetTracker, LLMError, make_llm
    from jobsearcher.ranking import load_ranking_config, run_ranking

    if not config.cv_path.exists():
        print(f"Master CV not found at {config.cv_path}", file=sys.stderr)
        return 2
    if not config.ranking_config.exists():
        print(f"Ranking config not found at {config.ranking_config}", file=sys.stderr)
        return 2
    store = Store(config.db_path)
    ranking_config = load_ranking_config(config.ranking_config)
    try:
        llm = make_llm(config, "ranking", BudgetTracker(store, config.llm))
    except LLMError as exc:
        print(f"Can't start ranking: {exc}", file=sys.stderr)
        return 2
    report = run_ranking(
        store,
        llm,
        ranking_config,
        config.cv_path.read_text(),
        max_parallel=config.llm.ranking.max_parallel,
    )
    print(
        f"rank: candidates={report.candidates} (prefilter dropped {report.skipped_prefilter}) "
        f"ranked={report.ranked} cached={report.cached} failed={report.failed} "
        f"deferred={report.deferred}"
    )
    if report.stopped_reason:
        print(f"stopped early: {report.stopped_reason}", file=sys.stderr)
    return 1 if report.failed and not report.ranked else 0


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    """One full pipeline pass: search, then rank; news signals when they're due."""
    from jobsearcher.signals.run import due

    search_status = cmd_search(config, args)
    if search_status == 2:
        return search_status
    status = max(search_status, cmd_rank(config, args))
    if config.sources.companies and config.companies_config.is_file():
        if due(Store(config.db_path), config.companies.signals_every_days):
            signals_args = argparse.Namespace(
                digest_only=False, days=None, min_relevance=40, limit=10
            )
            status = max(status, cmd_signals(config, signals_args))
    return status


def cmd_list(config: Config, args: argparse.Namespace) -> int:
    from jobsearcher.ranking import load_ranking_config, ranked_jobs

    store = Store(config.db_path)
    ranking_config = load_ranking_config(config.ranking_config)
    scores = {job.id: score for job, _, score in ranked_jobs(store, ranking_config)}
    status = None if args.all else JobStatus.OPEN
    jobs = list(store.iter_jobs(status))
    # Ranked jobs first (best first), then unranked by date.
    jobs.sort(key=lambda j: scores.get(j.id, -1), reverse=True)
    for job in jobs[: args.limit]:
        score = f"{scores[job.id]:3d}" if job.id in scores else "  -"
        sources = ",".join(s.source for s in job.sources)
        deadline = job.deadline.date().isoformat() if job.deadline else "-"
        contact = "✉" if job.contacts else " "
        print(
            f"{score}  {job.id}  {contact} {deadline:10}  {job.title[:45]:45}  "
            f"{(job.company or '')[:28]:28}  {job.location or '':14} [{sources}]"
        )
    return 0


def cmd_show(config: Config, args: argparse.Namespace) -> int:
    store = Store(config.db_path)
    job = store.get_job(args.job_id)
    if job is None:
        print(f"No job {args.job_id}", file=sys.stderr)
        return 1
    from jobsearcher.ranking import load_ranking_config
    from jobsearcher.ranking.ranker import job_details

    out = job_details(store, job, load_ranking_config(config.ranking_config))
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_occupations(config: Config, args: argparse.Namespace) -> int:
    """Occupation fields and groups of open jobs per target role, to help write
    `exclude_occupations` / `include_occupations` in ranking.yaml."""
    from collections import Counter

    from jobsearcher.ranking import load_ranking_config
    from jobsearcher.ranking.prefilter import matched_roles

    ranking_config = load_ranking_config(config.ranking_config)
    roles = {role.name: role for role in ranking_config.target_roles}
    counts: dict[str, Counter[tuple[str | None, str | None]]] = {n: Counter() for n in roles}
    for job in Store(config.db_path).iter_jobs():
        for name in matched_roles(job, ranking_config, apply_occupation_filters=False)[0]:
            counts[name][(job.occupation_field, job.occupation_group)] += 1

    for name, counter in counts.items():
        role = roles[name]
        kept = sum(n for (f, g), n in counter.items() if role.allows_occupation(f, g))
        print(f"\n{name}: {sum(counter.values())} open jobs, {kept} pass the occupation filters")
        by_field: Counter[str | None] = Counter()
        for (field, _), n in counter.items():
            by_field[field] += n
        for field, n in by_field.most_common():
            print(f"  {n:4}  {field or '(none)'}")
            if args.groups:
                for (f, group), m in counter.most_common():
                    if f == field:
                        mark = "" if role.allows_occupation(f, group) else "  [excluded]"
                        print(f"        {m:4}  {group or '(none)'}{mark}")
    return 0


def cmd_companies(config: Config, args: argparse.Namespace) -> int:
    """Target companies with their detected ATS and open jobs; --detect re-checks."""
    from collections import Counter
    from datetime import UTC, datetime

    from jobsearcher.companies import load_companies
    from jobsearcher.companies.crawl import resolve_feeds
    from jobsearcher.companies.http import PoliteClient
    from jobsearcher.sources.ats.common import feed_source

    if not config.companies_config.is_file():
        print(f"No company list at {config.companies_config}", file=sys.stderr)
        return 2
    companies = load_companies(config.companies_config)
    store = Store(config.db_path)
    if args.detect:
        client = PoliteClient(min_interval_s=config.companies.min_request_interval_s)
        _, detected = resolve_feeds(
            companies, store, client, config.companies, datetime.now(UTC), force=args.force
        )
        print(f"detected {detected} of {len(companies)} companies", file=sys.stderr)

    rows = store.company_ats()
    open_jobs: Counter[str] = Counter(
        s.source for job in store.iter_jobs() for s in job.sources if ":" in s.source
    )
    by_type: Counter[str] = Counter()
    for company in companies:
        row = rows.get(company.slug)
        ats = company.ats.type if company.ats else (row["ats_type"] if row else None)
        ref = company.ats.ref if company.ats else (row["ats_ref"] if row else None)
        by_type[ats or ("not checked" if row is None else "none found")] += 1
        jobs = open_jobs.get(feed_source(ats, ref), 0) if ats and ref else 0
        note = "" if ats else (row["error"] or "") if row else "not checked yet"
        print(f"{company.name[:34]:34} {ats or '-':15} {jobs:4}  {(ref or note)[:70]}")
    print("\n" + ", ".join(f"{t}: {n}" for t, n in by_type.most_common()))
    return 0


def cmd_signals(config: Config, args: argparse.Namespace) -> int:
    """Fetch and classify news about target companies, then print the digest."""
    from datetime import UTC, datetime

    from jobsearcher.companies import load_companies
    from jobsearcher.companies.http import PoliteClient
    from jobsearcher.llm import BudgetTracker, LLMError, make_llm
    from jobsearcher.ranking import load_ranking_config
    from jobsearcher.ranking.ranker import build_context
    from jobsearcher.signals.classify import classify_news
    from jobsearcher.signals.run import RUN_KEY, digest, fetch_all_news

    if not config.companies_config.is_file():
        print(f"No company list at {config.companies_config}", file=sys.stderr)
        return 2
    companies = load_companies(config.companies_config)
    store = Store(config.db_path)
    days = args.days or config.companies.news_days

    if not args.digest_only:
        client = PoliteClient(min_interval_s=config.companies.min_request_interval_s)
        fetched = fetch_all_news(store, client, companies, days)
        print(
            f"news: {fetched.companies} companies, {fetched.new_items} new items"
            + (f", failed: {', '.join(fetched.failed)}" if fetched.failed else ""),
            file=sys.stderr,
        )
        if not config.cv_path.exists():
            print(f"Master CV not found at {config.cv_path}", file=sys.stderr)
            return 2
        try:
            llm = make_llm(config, "ranking", BudgetTracker(store, config.llm))
        except LLMError as exc:
            print(f"Can't classify news: {exc}", file=sys.stderr)
            return 2
        context = build_context(
            config.cv_path.read_text(), load_ranking_config(config.ranking_config)
        )
        report = classify_news(
            store, llm, companies, context, max_parallel=config.llm.ranking.max_parallel
        )
        print(
            f"signals: {report.classified} of {report.items} items classified"
            + (f", {report.failed_batches} batches failed" if report.failed_batches else "")
            + (f"; stopped: {report.stopped_reason}" if report.stopped_reason else ""),
            file=sys.stderr,
        )
        store.set_last_run(RUN_KEY, datetime.now(UTC))

    for entry in digest(store, companies, days, min_relevance=args.min_relevance)[: args.limit]:
        jobs = f", {entry.open_jobs} open jobs" if entry.open_jobs else ""
        print(f"\n{entry.score:3d}  {entry.company.name}{jobs}")
        for row in entry.signals[:3]:
            date = (row["published_at"] or "")[:10]
            print(f"     {row['relevance']:3d} {row['kind']:18} {date}  {row['summary']}")
            print(f"         {row['url']}")
    return 0


def cmd_llm_check(config: Config, args: argparse.Namespace) -> int:
    """Send one tiny request to each configured model to verify keys and pricing."""
    from pydantic import BaseModel

    from jobsearcher.llm import BudgetTracker, LLMError, make_llm

    class Pong(BaseModel):
        reply: str

    tracker = BudgetTracker(Store(config.db_path), config.llm)
    status = 0
    for role in ("ranking", "drafting"):
        spec = getattr(config.llm, role)
        try:
            llm = make_llm(config, role, tracker)
            result = llm.complete(
                system="You are a connectivity check.",
                prompt='Reply with {"reply": "pong"}.',
                schema=Pong,
            )
        except LLMError as exc:
            print(f"{role:9} {spec.provider}/{spec.model}: FAILED: {exc}")
            status = 1
            continue
        u = result.usage
        cost = f"${tracker.cost(u):.5f}" if u.billed else "subscription"
        # Claude Code usage is recorded as "claude-code/<model>"; don't repeat the provider.
        model = u.model.removeprefix("claude-code/")
        print(
            f"{role:9} {spec.provider}/{model}: ok "
            f"({u.input_tokens} in / {u.output_tokens} out, {cost})"
        )
    return status


def cmd_budget(config: Config, args: argparse.Namespace) -> int:
    from jobsearcher.llm import BudgetTracker

    tracker = BudgetTracker(Store(config.db_path), config.llm)
    spent = tracker.month_to_date()
    print(
        f"LLM spend this month: ${spent:.2f} of ${config.llm.monthly_budget_usd:.2f} "
        f"(drafting pauses at ${tracker.limit_for('drafting'):.2f})"
    )
    calls, tokens = tracker.subscription_usage()
    if calls:
        print(f"Claude subscription (Claude Code): {calls} calls, {tokens:,} tokens this month")
    return 0


def cmd_web(config: Config, args: argparse.Namespace) -> int:
    """Serve the local web UI."""
    import uvicorn

    from jobsearcher.web import create_app

    host = args.host or config.web.host
    port = args.port or config.web.port
    if args.reload:
        # Reload needs an import string, so the factory re-reads config.yaml itself.
        if args.config:
            os.environ["JOBSEARCHER_CONFIG"] = args.config
        uvicorn.run(
            "jobsearcher.web:create_app_from_env",
            factory=True,
            host=host,
            port=port,
            reload=True,
            workers=1,
        )
    else:
        uvicorn.run(
            create_app(config, config_file_path(args.config)), host=host, port=port, workers=1
        )
    return 0


def seconds_until(daily_at: str, tz: ZoneInfo, now: datetime | None = None) -> float:
    now = now or datetime.now(tz)
    hour, minute = (int(x) for x in daily_at.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def cmd_daemon(config: Config, args: argparse.Namespace) -> int:
    """Run the pipeline once at startup (unless --no-initial-run), then daily."""
    tz = ZoneInfo(config.schedule.timezone)
    if not args.no_initial_run:
        cmd_run(config, args)
    while True:
        wait = seconds_until(config.schedule.daily_at, tz)
        log.info("Next run in %.1f h", wait / 3600)
        time.sleep(wait)
        config = load_config(args.config)  # pick up config edits without a restart
        try:
            cmd_run(config, args)
        except Exception:
            log.exception("Scheduled run failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobsearcher")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("search", help="fetch jobs from all enabled sources")
    sub.add_parser("rank", help="score unranked open jobs against the master CV")
    sub.add_parser("run", help="search, then rank (what the daemon does daily)")

    p_list = sub.add_parser("list", help="list stored jobs")
    p_list.add_argument("--all", action="store_true", help="include expired jobs")
    p_list.add_argument("--limit", type=int, default=50)

    p_show = sub.add_parser("show", help="print one job as JSON")
    p_show.add_argument("job_id")

    p_occ = sub.add_parser(
        "occupations", help="occupation fields of open jobs per target role (for filters)"
    )
    p_occ.add_argument("--groups", action="store_true", help="also list occupation groups")

    p_comp = sub.add_parser(
        "companies", help="target companies: detected ATS and open jobs (companies.yaml)"
    )
    p_comp.add_argument("--detect", action="store_true", help="detect ATS for unchecked/stale")
    p_comp.add_argument("--force", action="store_true", help="with --detect: re-check all")

    p_sig = sub.add_parser(
        "signals", help="news about target companies: fetch, classify, print the digest"
    )
    p_sig.add_argument("--digest-only", action="store_true", help="skip fetching/classifying")
    p_sig.add_argument("--days", type=int, help="news window (default companies.news_days)")
    p_sig.add_argument("--min-relevance", type=int, default=40)
    p_sig.add_argument("--limit", type=int, default=25)

    sub.add_parser("llm-check", help="send a tiny test request to each configured model")
    sub.add_parser("budget", help="show LLM spend this month")

    p_web = sub.add_parser("web", help="serve the local web UI")
    p_web.add_argument("--host", help="interface to bind (default: web.host in config.yaml)")
    p_web.add_argument("--port", type=int, help="port (default: web.port in config.yaml)")
    p_web.add_argument("--reload", action="store_true", help="restart on code changes (dev)")

    p_daemon = sub.add_parser("daemon", help="run the pipeline on the configured daily schedule")
    p_daemon.add_argument("--no-initial-run", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)
    handler = {
        "search": cmd_search,
        "rank": cmd_rank,
        "run": cmd_run,
        "list": cmd_list,
        "show": cmd_show,
        "occupations": cmd_occupations,
        "companies": cmd_companies,
        "signals": cmd_signals,
        "llm-check": cmd_llm_check,
        "budget": cmd_budget,
        "daemon": cmd_daemon,
        "web": cmd_web,
    }
    return handler[args.command](config, args)


if __name__ == "__main__":
    sys.exit(main())
