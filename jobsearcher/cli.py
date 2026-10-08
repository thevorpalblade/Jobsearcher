"""Command-line entry point: `jobsearcher <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from jobsearcher.config import DEFAULT_PROFILE, Config, config_file_path, load_config
from jobsearcher.cvs import ranking_cv
from jobsearcher.models import JobStatus
from jobsearcher.pipeline import all_profiles, matches_filters, profiles_keywords, run_search
from jobsearcher.store import Store

log = logging.getLogger("jobsearcher")


def cmd_search(config: Config, args: argparse.Namespace) -> int:
    """One search for every profile: the job pool is shared."""
    profiles = all_profiles(config)
    keywords = profiles_keywords(profiles)
    if not keywords:
        print(
            "No keywords: set search.keywords (config.yaml or a profile.yaml) or "
            "target_roles in ranking.yaml",
            file=sys.stderr,
        )
        return 2
    store = Store(config.db_path, profile=config.profile)
    report = run_search(config, store, keywords=keywords, profiles=profiles)
    print(
        f"search: {len(keywords)} keywords, fetched={report.fetched} "
        f"filtered_out={report.filtered_out} new={report.new} updated={report.updated} "
        f"expired={report.expired} open_total={store.count_jobs(JobStatus.OPEN)}"
    )
    if report.failed_sources:
        print(f"failed sources: {', '.join(report.failed_sources)}", file=sys.stderr)
        return 1
    return 0


# Exit status of `rank` / `signals` when a provider failure streak stopped them early:
# the daemon retries these after a pause.
RETRY = 3


def cmd_rank(config: Config, args: argparse.Namespace) -> int:
    from jobsearcher.llm import BudgetTracker, LLMError, make_llm
    from jobsearcher.ranking import load_ranking_config, run_ranking

    cv = ranking_cv(config.cv_path)
    if cv is None:
        print(f"Master CV not found at {config.cv_path}", file=sys.stderr)
        return 2
    if not config.ranking_config.exists():
        print(f"Ranking config not found at {config.ranking_config}", file=sys.stderr)
        return 2
    store = Store(config.db_path, profile=config.profile)
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
        cv,
        max_parallel=config.llm.ranking.max_parallel,
        wanted=lambda job: matches_filters(job, config.search),
    )
    print(
        f"rank{_profile_label(config)}: candidates={report.candidates} "
        f"(prefilter dropped {report.skipped_prefilter}) "
        f"ranked={report.ranked} cached={report.cached} failed={report.failed} "
        f"deferred={report.deferred}"
    )
    if report.stopped_reason:
        print(f"stopped early: {report.stopped_reason}", file=sys.stderr)
    if report.stopped_on_failures:
        return RETRY
    return 1 if report.failed and not report.ranked else 0


def _profile_label(config: Config) -> str:
    return "" if config.profile == DEFAULT_PROFILE else f" [{config.profile}]"


def _profiles(config: Config, args: argparse.Namespace) -> list[Config]:
    """The profiles a run covers: --profile, else every one."""
    if getattr(args, "profile", None):
        return [config.for_profile(args.profile)]
    return all_profiles(config)


def run_pipeline(config: Config, args: argparse.Namespace) -> tuple[int, bool]:
    """One full pass: search (once, for every profile), then for each profile rank and
    news signals (fetched weekly; leftovers are classified every pass). Returns (exit
    status, retry): retry is True when ranking or news classification stopped for any
    profile because the provider kept failing."""
    from jobsearcher.signals.run import due

    search_status = cmd_search(config, args)
    if search_status == 2:
        return search_status, False
    status, retry = search_status, False
    for profile in _profiles(config, args):
        rank_status = cmd_rank(profile, args)
        retry = retry or rank_status == RETRY
        status = max(status, 0 if rank_status == RETRY else rank_status)
        if config.sources.companies and profile.companies_config.is_file():
            store = Store(profile.db_path, profile=profile.profile)
            fetch = due(store, config.companies.signals_every_days)
            signals_args = argparse.Namespace(
                digest_only=False,
                days=None,
                min_relevance=40,
                limit=10 if fetch else 0,
                fetch=fetch,
            )
            signals_status = cmd_signals(profile, signals_args)
            retry = retry or signals_status == RETRY
            status = max(status, 0 if signals_status == RETRY else signals_status)
        try:  # contact people for the best jobs; never fails the run
            cmd_contacts(profile, argparse.Namespace(job_id=None, company=None))
        except Exception:
            log.exception("Contact lookups failed")
    return status, retry


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    """One full pipeline pass: search, then rank; news signals when they're due."""
    return run_pipeline(config, args)[0]


def cmd_list(config: Config, args: argparse.Namespace) -> int:
    from jobsearcher.ranking import load_ranking_config, ranked_jobs

    store = Store(config.db_path, profile=config.profile)
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
    store = Store(config.db_path, profile=config.profile)
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
    for job in Store(config.db_path, profile=config.profile).iter_jobs():
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
    store = Store(config.db_path, profile=config.profile)
    if args.detect:
        client = PoliteClient.from_config(config)
        _, detected = resolve_feeds(
            companies,
            store,
            client,
            config.companies,
            datetime.now(UTC),
            force=args.force,
            retry_failed=args.failed,
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
    from jobsearcher.signals.run import digest, fetch_all_news, run_key

    if not config.companies_config.is_file():
        print(f"No company list at {config.companies_config}", file=sys.stderr)
        return 2
    companies = load_companies(config.companies_config)
    store = Store(config.db_path, profile=config.profile)
    days = args.days or config.companies.news_days
    status = 0
    if not args.digest_only and not getattr(args, "fetch", True):
        from jobsearcher.signals.classify import PROMPT_VERSION as SIGNALS_VERSION

        if not store.unclassified_news(SIGNALS_VERSION):
            return 0  # nothing left over: no need to build a model client

    if not args.digest_only:
        if getattr(args, "fetch", True):  # fetching is weekly; classifying leftovers isn't
            client = PoliteClient.from_config(config)
            fetched = fetch_all_news(store, client, companies, days, config.news_source)
            print(
                f"news ({config.news_source}): {fetched.companies} companies, "
                f"{fetched.new_items} new items"
                + (f", failed: {', '.join(fetched.failed)}" if fetched.failed else ""),
                file=sys.stderr,
            )
            store.set_last_run(run_key(store), datetime.now(UTC))
        cv = ranking_cv(config.cv_path)
        if cv is None:
            print(f"Master CV not found at {config.cv_path}", file=sys.stderr)
            return 2
        try:
            llm = make_llm(config, "ranking", BudgetTracker(store, config.llm))
        except LLMError as exc:
            print(f"Can't classify news: {exc}", file=sys.stderr)
            return 2
        context = build_context(cv, load_ranking_config(config.ranking_config))
        report = classify_news(
            store, llm, companies, context, max_parallel=config.llm.ranking.max_parallel
        )
        print(
            f"signals: {report.classified} of {report.items} items classified"
            + (f", {report.failed_batches} batches failed" if report.failed_batches else "")
            + (f"; stopped: {report.stopped_reason}" if report.stopped_reason else ""),
            file=sys.stderr,
        )
        if report.stopped_on_failures:
            status = RETRY

    for entry in digest(store, companies, days, min_relevance=args.min_relevance)[: args.limit]:
        jobs = f", {entry.open_jobs} open jobs" if entry.open_jobs else ""
        print(f"\n{entry.score:3d}  {entry.company.name}{jobs}")
        for row in entry.signals[:3]:
            date = (row["published_at"] or "")[:10]
            print(f"     {row['relevance']:3d} {row['kind']:18} {date}  {row['summary']}")
            print(f"         {row['url']}")
    return status


def cmd_draft(config: Config, args: argparse.Namespace) -> int:
    """Draft a tailored CV and cover letter for a job, or a spontaneous application to a
    company (--company); checked against the CVs. Prints where the files are."""
    from jobsearcher.companies import load_companies
    from jobsearcher.companies.config import slugify
    from jobsearcher.drafting.service import DraftError, draft_company, draft_job, drafts_dir
    from jobsearcher.llm import LLMError

    store = Store(config.db_path, profile=config.profile)
    try:
        if args.company:
            wanted = slugify(args.company)
            companies = [c for c in load_companies(config.companies_config) if c.slug == wanted]
            if not companies:
                print(f"No company {args.company!r} in {config.companies_config}", file=sys.stderr)
                return 1
            draft = draft_company(
                config, store, companies[0], args.instructions, args.cv, force=args.force
            )
        elif args.job_id:
            draft = draft_job(
                config, store, args.job_id, args.instructions, args.cv, force=args.force
            )
        else:
            print("Give a job id, or --company NAME", file=sys.stderr)
            return 2
    except (DraftError, LLMError) as exc:
        print(f"Draft failed: {exc}", file=sys.stderr)
        return 1
    folder = drafts_dir(config) / draft.key.replace(":", "-") / draft.input_hash
    print(
        f"draft {draft.input_hash} for {draft.key}: "
        + ("NEEDS REVIEW" if draft.needs_review else "ready")
    )
    print(f"  files: {folder}  ({', '.join(draft.files)})")
    print(
        f"  checked {draft.claims_checked} claims" + (", repaired once" if draft.repaired else "")
    )
    if draft.check_error:
        print(f"  {draft.check_error}")
    for claim in draft.flagged:
        print(f"  UNSUPPORTED: {claim.claim}")
    for note in draft.notes:
        print(f"  note: {note}")
    return 0


def cmd_drafts(config: Config, args: argparse.Namespace) -> int:
    """List stored drafts, newest first."""
    from jobsearcher.drafting.core import Draft

    store = Store(config.db_path, profile=config.profile)
    rows = sorted(store.latest_drafts().values(), key=lambda r: r["created_at"], reverse=True)
    for row in rows:
        draft = Draft.model_validate_json(row["data"])
        job = store.get_job(draft.key)
        title = f"{job.title} — {job.company}" if job else draft.key
        flag = "needs review" if draft.needs_review else "ready"
        print(f"{draft.created_at:%Y-%m-%d %H:%M}  {flag:12} {draft.key}  {title[:70]}")
    return 0


def cmd_llm_check(config: Config, args: argparse.Namespace) -> int:
    """Send one tiny request to each configured model to verify keys and pricing."""
    from pydantic import BaseModel

    from jobsearcher.llm import BudgetTracker, LLMError, make_llm

    class Pong(BaseModel):
        reply: str

    tracker = BudgetTracker(Store(config.db_path, profile=config.profile), config.llm)
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

    for profile in _profiles(config, args):  # each profile has its own budget
        store = Store(profile.db_path, profile=profile.profile)
        tracker = BudgetTracker(store, profile.llm)
        spent = tracker.month_to_date()
        print(
            f"LLM spend this month{_profile_label(profile)}: ${spent:.2f} of "
            f"${profile.llm.monthly_budget_usd:.2f} "
            f"(drafting pauses at ${tracker.limit_for('drafting'):.2f})"
        )
        calls, tokens = tracker.subscription_usage()
        if calls:
            print(f"  Claude subscription (Claude Code): {calls} calls, {tokens:,} tokens")
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
    elif config.web.public_port:
        # One process, two listeners: the home network's, and the internet-facing one that
        # only a reverse proxy on this machine reaches (the app tells them apart by port).
        # proxy_headers=False: the app trusts X-Forwarded-For itself, on the public one only.
        import socket

        sockets = []
        for bind_host, bind_port in ((host, port), ("127.0.0.1", config.web.public_port)):
            sock = socket.socket(socket.AF_INET6 if ":" in bind_host else socket.AF_INET)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_host, bind_port))
            sockets.append(sock)
        app = create_app(config, config_file_path(args.config))
        server = uvicorn.Server(uvicorn.Config(app, workers=1, proxy_headers=False))
        log.info(
            "Listening on %s:%d and, for the proxy, 127.0.0.1:%d",
            host,
            port,
            config.web.public_port,
        )
        server.run(sockets=sockets)
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


def retry_stalled(config: Config, args: argparse.Namespace) -> bool:
    """Ranking and news classification again (every profile), after a provider failure
    streak stopped them. Never searches or fetches news again. True if they are still
    stalled for any profile."""
    stalled = False
    for profile in _profiles(config, args):
        stalled = cmd_rank(profile, args) == RETRY or stalled
        if config.sources.companies and profile.companies_config.is_file():
            signals_args = argparse.Namespace(
                digest_only=False, days=None, min_relevance=40, limit=0, fetch=False
            )
            stalled = cmd_signals(profile, signals_args) == RETRY or stalled
    return stalled


def daemon_cycle(
    config: Config,
    args: argparse.Namespace,
    tz: ZoneInfo,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """One daily run, then, while a failing provider keeps ranking stalled, a retry every
    `schedule.retry_minutes` (up to `schedule.retries`, and not into the next daily run)."""
    _, stalled = run_pipeline(config, args)
    attempt = 0
    while stalled and attempt < config.schedule.retries:
        pause = config.schedule.retry_minutes * 60
        if pause <= 0 or seconds_until(config.schedule.daily_at, tz) <= pause:
            break  # retries are off, or the next daily run is about to start
        attempt += 1
        log.info(
            "The provider is failing: ranking retries in %d minutes (attempt %d of %d)",
            config.schedule.retry_minutes,
            attempt,
            config.schedule.retries,
        )
        sleep(pause)
        config = load_config(args.config)  # a config fix (e.g. another model) applies at once
        try:
            stalled = retry_stalled(config, args)
        except Exception:
            log.exception("Retry failed")
            break


def cmd_contacts(config: Config, args: argparse.Namespace) -> int:
    """Look up contact people on employers' websites (docs/m5-contacts.md): for one job
    or company, or (with neither) for this profile's best and shortlisted jobs."""
    from jobsearcher.companies.config import slugify
    from jobsearcher.contacts import service as contacts
    from jobsearcher.contacts.site import company_key
    from jobsearcher.ranking import load_ranking_config

    store = Store(config.db_path, profile=config.profile)
    job_id = getattr(args, "job_id", None)
    company_name = getattr(args, "company", None)
    if job_id or company_name:
        if company_name:
            from jobsearcher.companies import load_companies

            wanted = slugify(company_name)
            matches = [c for c in load_companies(config.companies_config) if c.slug == wanted]
            if not matches:
                print(f"No company {company_name!r} in {config.companies_config}", file=sys.stderr)
                return 1
            found = contacts.for_company(config, store, matches[0], force=True)
        else:
            found = contacts.for_job(config, store, job_id, force=True)
        for c in found.contacts:
            guess = f"  (guessed: {c.guessed_email})" if c.guessed_email else ""
            print(f"{c.name}, {c.role or '-'} <{c.email or '-'}>{guess}")
            print(f"    {c.note or ''}\n    {c.url}")
        if found.error:
            print(found.error, file=sys.stderr)
        return 0
    rc = load_ranking_config(config.ranking_config)
    if not rc.contacts.auto:
        return 0
    due = contacts.due_for_lookup(store, rc)
    if not due:
        return 0
    clients = contacts.make_clients(config, store)
    companies: set[str] = set()
    looked = found_people = 0
    for job in due:
        key = company_key(job.company or "")
        if key not in companies and len(companies) >= rc.contacts.max_companies_per_run:
            continue  # the rest wait for the next run (other jobs at known companies don't)
        companies.add(key)
        try:
            result = contacts.for_job(config, store, job.id, clients=clients)
        except Exception:  # one site failing mustn't stop the others
            log.exception("Contact lookup for %s failed", job.id)
            continue
        looked += 1
        found_people += bool(result.contacts)
    print(
        f"contacts{_profile_label(config)}: {looked} jobs looked up at {len(companies)} "
        f"companies, people found for {found_people}; {len(due) - looked} wait"
    )
    return 0


def cmd_migrate_profiles(config: Config, args: argparse.Namespace) -> int:
    """Move a setup without profiles into profiles/<slug>/: ranking.yaml,
    companies.yaml, the CVs and config.yaml's search section, plus the database rows
    and drafts. Run it once, with the daemon and web UI stopped."""
    import shutil

    import yaml

    from jobsearcher.companies.config import slugify
    from jobsearcher.config import PROFILE_FILE

    slug = slugify(args.slug)
    if not slug or slug != args.slug or slug == DEFAULT_PROFILE:
        print(f"Pick a plain lowercase name, not {args.slug!r}", file=sys.stderr)
        return 2
    if config.profile_slugs() != [DEFAULT_PROFILE]:
        print(f"Profiles already exist in {config.profiles_dir}", file=sys.stderr)
        return 2
    folder = config.profiles_dir / slug
    moves = [
        (config.ranking_config, folder / "ranking.yaml"),
        (config.companies_config, folder / "companies.yaml"),
        (config.cv_path.parent, folder / "cvs"),
    ]
    if config.cv_path.name != "master.md":
        print(f"The master CV must be named master.md, not {config.cv_path.name}", file=sys.stderr)
        return 2
    store = Store(config.db_path)  # migrates the schema first, if needed
    folder.mkdir(parents=True)
    for source, target in moves:
        if source.exists():
            shutil.move(source, target)
            print(f"moved {source} -> {target}")
    search = config.search.model_dump(mode="json", exclude={"expire_after_days"})
    settings = {"name": config.web.user_name, "search": search}
    (folder / PROFILE_FILE).write_text(
        "# This candidate's own settings (docs/m10-multi-user.md). search: replaces\n"
        "# config.yaml's search section, except expire_after_days.\n"
        + yaml.safe_dump(settings, allow_unicode=True, sort_keys=False)
    )
    print(f"wrote {folder / PROFILE_FILE}")
    store.rename_profile(DEFAULT_PROFILE, slug)
    print(f"database rows now belong to {slug!r}")
    # The existing setup keeps using .env's keys and any provider (e.g. claude_code).
    from jobsearcher.settings import merge_yaml

    config_path = config_file_path(args.config)
    text = config_path.read_text() if config_path.is_file() else ""
    keyed = [*config.llm.server_key_profiles, slug]
    config_path.write_text(merge_yaml(text, {"llm": {"server_key_profiles": keyed}}))
    print(f"{config_path.name}: llm.server_key_profiles now includes {slug!r} (uses .env's keys)")
    drafts = config.data_dir / "drafts"
    if drafts.is_dir() and any(drafts.iterdir()):
        target = drafts / slug
        target.mkdir()
        for entry in list(drafts.iterdir()):
            if entry != target:
                shutil.move(entry, target / entry.name)
        print(f"moved drafts into {target}")
    print(
        "Done. config.yaml's search section now only sets expire_after_days (edit the rest "
        "in the profile.yaml), and its cv_path, ranking_config and companies_config are "
        "no longer used. Restart the daemon and the web UI."
    )
    return 0


def cmd_users(config: Config, args: argparse.Namespace) -> int:
    """Web UI accounts: add, invite (a new set-password link), disable, enable, list."""
    from jobsearcher.auth import ADMIN, USER, Auth, AuthError

    store = Store(config.db_path)
    auth = Auth(store.conn)
    host = config.web.host if config.web.host not in ("0.0.0.0", "::") else "localhost"
    base_url = (args.url or f"http://{host}:{config.web.port}").rstrip("/")

    def link(token: str) -> None:
        print(f"Set-password link (works once, for 24 hours): {base_url}/invite/{token}")

    if args.action == "list":
        for row in auth.users():
            state = (
                "disabled" if row["disabled"] else ("active" if row["has_password"] else "invited")
            )
            seen = (row["last_seen"] or "never")[:16]
            print(
                f"{row['username']:20} {row['role']:6} {row['profile'] or '-':12} {state:9} "
                f"last seen {seen}"
            )
        return 0
    if not args.name:
        print("Give a username", file=sys.stderr)
        return 2
    try:
        if args.action == "add":
            slugs = config.profile_slugs()
            profile = args.user_profile or (slugs[0] if args.admin else None)
            if profile not in slugs:
                print(
                    f"Pick the candidate profile with --profile (one of: {', '.join(slugs)})",
                    file=sys.stderr,
                )
                return 2
            _, token = auth.create_user(args.name, ADMIN if args.admin else USER, profile)
            print(f"Added {args.name} ({'admin' if args.admin else 'user'}, profile {profile}).")
            link(token)
            return 0
        user = auth.user_by_name(args.name)
        if user is None:
            print(f"No user {args.name!r}", file=sys.stderr)
            return 1
        if args.action == "invite":
            link(auth.new_invite(user))
        elif args.action == "reset-2fa":
            auth.reset_totp(user)
            print(f"Two-factor codes turned off for {user.username}; they can set them up again.")
        else:
            disable = args.action == "disable"
            auth.set_disabled(user, disable)
            print(
                f"{user.username} "
                + ("disabled (logged out everywhere)." if disable else "enabled.")
            )
    except AuthError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def cmd_daemon(config: Config, args: argparse.Namespace) -> int:
    """Run the pipeline once at startup (unless --no-initial-run), then daily."""
    tz = ZoneInfo(config.schedule.timezone)
    if not args.no_initial_run:
        daemon_cycle(config, args, tz)
    while True:
        wait = seconds_until(config.schedule.daily_at, tz)
        log.info("Next run in %.1f h", wait / 3600)
        time.sleep(wait)
        config = load_config(args.config)  # pick up config edits without a restart
        try:
            daemon_cycle(config, args, tz)
        except Exception:
            log.exception("Scheduled run failed")


# Commands that work on every profile (or none) rather than one.
SHARED_COMMANDS = {"search", "run", "daemon", "web", "users", "migrate-profiles", "budget"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobsearcher")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--profile", help="which candidate's profile (default: the first)")
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
    p_comp.add_argument(
        "--failed",
        action="store_true",
        help="with --detect: re-check companies where no ATS was found (e.g. after "
        "changing crawl settings)",
    )

    p_sig = sub.add_parser(
        "signals", help="news about target companies: fetch, classify, print the digest"
    )
    p_sig.add_argument("--digest-only", action="store_true", help="skip fetching/classifying")
    p_sig.add_argument("--days", type=int, help="news window (default companies.news_days)")
    p_sig.add_argument("--min-relevance", type=int, default=40)
    p_sig.add_argument("--limit", type=int, default=25)

    p_draft = sub.add_parser(
        "draft", help="draft a tailored CV and cover letter for a job (or --company)"
    )
    p_draft.add_argument("job_id", nargs="?", help="job id (shown in the job page's URL)")
    p_draft.add_argument("--company", help="spontaneous application to a company in companies.yaml")
    p_draft.add_argument("--instructions", default="", help="what to emphasise, in your words")
    p_draft.add_argument("--cv", help="base the draft on this CV (a name from the CV list)")
    p_draft.add_argument("--force", action="store_true", help="draft again even if cached")
    sub.add_parser("drafts", help="list stored drafts")

    sub.add_parser("llm-check", help="send a tiny test request to each configured model")
    sub.add_parser("budget", help="show LLM spend this month")

    p_web = sub.add_parser("web", help="serve the local web UI")
    p_web.add_argument("--host", help="interface to bind (default: web.host in config.yaml)")
    p_web.add_argument("--port", type=int, help="port (default: web.port in config.yaml)")
    p_web.add_argument("--reload", action="store_true", help="restart on code changes (dev)")

    p_users = sub.add_parser("users", help="web UI accounts: add, invite, disable, enable, list")
    p_users.add_argument(
        "action", choices=["add", "invite", "disable", "enable", "reset-2fa", "list"]
    )
    p_users.add_argument("name", nargs="?", help="username")
    p_users.add_argument("--admin", action="store_true", help="with add: an admin account")
    p_users.add_argument(
        "--profile",
        dest="user_profile",
        metavar="SLUG",
        help="with add: the candidate profile they see",
    )
    p_users.add_argument(
        "--url", help="the web UI's address, for the link (default: http://<web.host>:<web.port>)"
    )

    p_contacts = sub.add_parser(
        "contacts",
        help="look up contact people on employers' websites (a job, --company, or the "
        "best and shortlisted jobs)",
    )
    p_contacts.add_argument("job_id", nargs="?", help="one job (default: the due ones)")
    p_contacts.add_argument("--company", help="a company in companies.yaml")

    p_migrate = sub.add_parser(
        "migrate-profiles", help="move this setup's files and data into profiles/<slug>/"
    )
    p_migrate.add_argument("slug", help="the candidate's profile name, e.g. their first name")

    p_daemon = sub.add_parser("daemon", help="run the pipeline on the configured daily schedule")
    p_daemon.add_argument("--no-initial-run", action="store_true")

    args = parser.parse_args(argv)
    # Line-buffer stdout: the daemon's output goes to a file, where Python would
    # otherwise hold the summary lines until exit (which a daemon never reaches).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)
    if args.command not in SHARED_COMMANDS:
        try:
            config = config.for_profile(args.profile)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
    handler = {
        "search": cmd_search,
        "rank": cmd_rank,
        "run": cmd_run,
        "list": cmd_list,
        "show": cmd_show,
        "occupations": cmd_occupations,
        "companies": cmd_companies,
        "signals": cmd_signals,
        "draft": cmd_draft,
        "drafts": cmd_drafts,
        "llm-check": cmd_llm_check,
        "budget": cmd_budget,
        "daemon": cmd_daemon,
        "web": cmd_web,
        "migrate-profiles": cmd_migrate_profiles,
        "users": cmd_users,
        "contacts": cmd_contacts,
    }
    return handler[args.command](config, args)


if __name__ == "__main__":
    sys.exit(main())
