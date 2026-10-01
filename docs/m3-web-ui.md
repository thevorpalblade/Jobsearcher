# M3: local web UI, implementation plan

Approved by the user on 2026-10-01 with the defaults in "Decisions" below.
Settled before this plan (see PLAN.md §6): FastAPI + Jinja + HTMX, served by
`jobsearcher web` on port 8080 as a second docker-compose service sharing the
`data/` volume with the daemon; LAN only (Tailscale/WireGuard for remote
access), so no auth in v1.

Measured on the live DB (636 open jobs, ~200 rankings): loading and parsing
all open jobs and rankings takes ~35 ms; `select_for_ranking` takes ~700 ms;
`PRAGMA journal_mode` is `delete` (WAL is not on).

## Decisions

1. Minimal application tracking (state + notes) is part of M3, built last.
2. The list shows all ranked jobs by score, with an "exclude Swedish required"
   filter (Swedish-required penalty is currently −10).
3. ~100 lines of hand-written CSS (no CSS framework).
4. Rankings made before a CV/roles/model change get a "stale" badge; the web
   container mounts `cvs/` read-only for that.
5. Compose publishes `${WEB_BIND:-0.0.0.0}:8080`; README warns that Docker's
   published ports bypass ufw/firewalld, so set `WEB_BIND` on a server with a
   public IP.
6. Expired jobs are hidden; `view=expired` shows the latest 200. Tracked jobs
   always show in `view=tracked`, even after expiry.
7. English UI; dates in `schedule.timezone` (Europe/Stockholm).
8. No TrustedHostMiddleware in v1.
9. M4's "Regenerate" (later) will queue a request that the daemon handles, so
   the web container never needs API keys or the Claude token.

## 1. SQLite concurrency

- `Store.__init__`: for file DBs, `PRAGMA journal_mode=WAL` (persists in the
  file). Switching needs exclusive access: catch `sqlite3.OperationalError`
  ("database is locked"), log at debug, continue; the next opener sets it.
  Optionally `PRAGMA synchronous=NORMAL`.
- Constructor gains keyword options: `readonly: bool = False`,
  `check_same_thread: bool = True`, `init_schema: bool = True`.
  `readonly=True` connects with `file:{path}?mode=ro` (uri=True) and skips the
  schema script, migrations and the WAL pragma.
- Web connections: one per request via a FastAPI dependency
  (`get_store` read-only; `get_writable_store` for POSTs, `init_schema=False`).
  Both need `check_same_thread=False`: FastAPI runs a sync generator
  dependency's setup/teardown and the sync endpoint as separate threadpool
  calls, which may land on different threads. Safe because a request uses its
  connection serially.
- App lifespan opens one normal writable `Store` at startup (creates DB/schema
  on a fresh install, runs migrations incl. the `applications` table, sets
  WAL), then closes it.
- `data/` is mounted read-write in the web container (WAL `-shm`/`-wal` files,
  tracking writes). One uvicorn worker.

## 2. Performance

- Keep scoring and filtering in Python (`final_score` reads ranking.yaml on
  read; the prefilter isn't expressible in SQL). Fine up to a few thousand jobs.
- Memoise the prefilter per job in the web process, keyed by
  `(job.id, job.content_hash, occupation_field, occupation_group,
  role_filter_fingerprint)`. `role_filter_fingerprint` hashes `target_roles`
  *including* occupation lists (`RankingConfig.fingerprint()` excludes them on
  purpose, so add `RankingConfig.filter_fingerprint()`).
- Optional: precompile one alternation regex per role in `prefilter.py`
  (`(?<!\w)(?:t1|t2…)(?!\w)`, cached by `tuple(terms)`): ~4× faster, same results.
- `ranked_jobs()` does N+1 `get_job` calls: rewrite on `iter_jobs()` +
  `latest_rankings()` joined in memory; same signature.
- Expired jobs are only loaded on demand
  (`... WHERE status='expired' ORDER BY last_seen DESC LIMIT ?`).

## 3. Pages

All routes are sync `def`. `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`.

### `GET /`: job list
- Full page normally; only the `_rows.html` fragment when `HX-Request` is set
  (and not `HX-History-Restore-Request`). Filter form:
  `hx-get="/" hx-target="#rows" hx-push-url="true"
  hx-trigger="change, keyup changed delay:300ms from:[name=q]"`; also works as
  a plain GET without JS.
- Filters: pydantic `ListFilters` via `Annotated[ListFilters, Query()]`, with a
  `mode="before"` validator turning `""` into `None` (else empty fields 422):
  `view` (ranked default | pending | excluded | all | expired | tracked),
  `min_score`, `source`, `location` (dropdown of distinct values; substring),
  `remote`, `deadline_within` (days; also hides past deadlines), `role`
  (assessment's matched_role, or prefilter roles for unranked), `language`,
  `swedish` (any | exclude required | not_mentioned only), `has_contact`, `new`
  (first_seen within N days, "new" badge), `q` (title/company), `sort` (score
  default | fit | success | deadline | published).
- Columns: score (badge at ≥ `drafting.min_score`), fit/success, title (link),
  company, location, remote, deadline as days left, language + Swedish flag,
  contact icon, sources, tracking state. `ignored` jobs hidden by default.

### `GET /jobs/{job_id}`: detail
- Header: title, company + org.nr, location/region, remote, employment type,
  salary, published, first/last seen, deadline.
- Apply: button for `apply_url`, `mailto:` for `apply_email`, every
  `SourceRef.url`. Only `http(s)` URLs become links (others shown as text);
  `rel="noopener noreferrer"`, `referrerpolicy="no-referrer"`.
- Score breakdown: final score, fit/success with weights, one line per
  adjustment. Add `score_breakdown(assessment, config) -> ScoreBreakdown` in
  `ranker.py`; `final_score` returns `score_breakdown(...).total`.
- Assessment: matched role, matched/missing requirements, red flags,
  rationale, language, Swedish, model, ranked-at. Unparseable (old prompt
  version) ranking → "will be re-ranked", never a 500. "Stale" badge when the
  stored `input_hash` ≠ `input_hash(job, cv, config, config.llm.ranking.model)`.
- Contacts: name, role, email (mailto), phone (tel) and provenance, with a
  readable label plus the raw value (`platsbanken:application_contacts` →
  "Platsbanken (structured)", `llm:ad_text` → "Named in the ad (extracted by
  the LLM, check before use)"); generic mailboxes marked.
- Prefilter diagnostics: roles matched in title/body, occupation field/group,
  passes or the exclusion reason; flag ranked-but-now-excluded jobs.
- Ad text: autoescaped, `white-space: pre-wrap` (untrusted input).
- Reserved `{% block drafts %}` (M4) and `{% block tracking %}`.

### Other routes
- `GET /jobs/{job_id}.json`: same output as `jobsearcher show`.
- `GET /prefilter`: web version of `jobsearcher occupations --groups`: per role
  mentioned/kept/excluded counts, fields and groups with [excluded], counts
  link to `/?view=excluded&role=…&occupation=…`. The excluded list view shows a
  reason column and title-vs-body-only match.
- Budget widget `_budget.html` in every page header and at
  `GET /partials/budget` (`hx-trigger="every 60s"`): `month_to_date()` vs
  `monthly_budget_usd` bar, marker at `limit_for("drafting")`,
  `subscription_usage()` calls/tokens.
- `GET /status`: last run per source, counts (open, ranked, pending, excluded,
  unparseable/stale), spend this month by model and purpose
  (`Store.llm_usage_summary(since)`, a GROUP BY on `llm_usage`).
- `GET /healthz`, `/static/*`.

### Tracking (step 10)
```sql
CREATE TABLE IF NOT EXISTS applications (
    job_id     TEXT PRIMARY KEY REFERENCES jobs(id),
    state      TEXT NOT NULL,   -- shortlisted|applied|interview|rejected|ignored ("new" = no row)
    notes      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
```
- `ApplicationState` StrEnum + `Application` model in `models.py`;
  `Store.applications()`, `get_application(job_id)`,
  `set_application(job_id, state, notes)`.
- `POST /jobs/{id}/state`, `POST /jobs/{id}/notes` return the updated
  fragment. They **require the `HX-Request` header (403 otherwise)**: a custom
  header forces a CORS preflight the app never allows, which blocks cross-site
  form posts without auth. Needs `python-multipart`.
- `view=tracked` includes expired tracked jobs (one extra query
  `WHERE id IN (SELECT job_id FROM applications)`).

## 4. Layout, dependencies, Docker, CLI

```
jobsearcher/web/__init__.py      # create_app(config: Config) -> FastAPI
jobsearcher/web/app.py           # factory, lifespan, dependencies, routes, Jinja filters
jobsearcher/web/views.py         # pure data shaping, no FastAPI: JobRow, ListFilters,
                                 #   load_rows, apply_filters, sort_rows, prefilter_summary,
                                 #   provenance_label, safe_url
jobsearcher/web/templates/       # base, list, _rows, job, _budget, _tracking, prefilter,
                                 #   status, 404
jobsearcher/web/static/htmx.min.js   # vendored, pinned 2.0.x, version in a header comment
jobsearcher/web/static/style.css
```
- `JobRow` dataclass: job, ranking | None, score | None, breakdown,
  prefilter (`PrefilterResult`), stage (ranked | pending | excluded),
  application | None, stale.
- `ranking.yaml` reloads on mtime change (`RankingConfigCache`); `config.yaml`
  read once at startup.
- `ranking/config.py`: `TargetRole.occupation_verdict(field, group) ->
  tuple[bool, str | None]` (reason), `allows_occupation` built on it;
  `RankingConfig.filter_fingerprint()`.
- `ranking/prefilter.py`: `prefilter_status(job, config) -> PrefilterResult(passed,
  roles, in_title, body_only, roles_unfiltered, excluded: dict[role, reason])`;
  `select_for_ranking` built on it.
- HTMX is vendored (works offline on the LAN, no third-party requests,
  pinned). Hatchling ships package data in the wheel; add a test that the
  templates/static files are packaged.
- `pyproject.toml` core deps: `fastapi>=0.115`, `jinja2>=3.1`, `uvicorn>=0.30`,
  `python-multipart>=0.0.9`.
- `config.py`: `WebConfig(host="127.0.0.1", port=8080)` and `Config.web`.
- `cli.py`: `web` subcommand (`--host`, `--port`, `--reload`); `cmd_web`
  imports uvicorn lazily, `uvicorn.run(create_app(config), host, port, workers=1)`.
- `Dockerfile`: `EXPOSE 8080`. `docker-compose.yml`: `image: jobsearcher:local`
  on the existing service, plus:
  ```yaml
    web:
      image: jobsearcher:local
      container_name: jobsearcher-web
      restart: unless-stopped
      command: ["web", "--host", "0.0.0.0", "--port", "8080"]
      environment: { TZ: Europe/Stockholm }
      ports: ["${WEB_BIND:-0.0.0.0}:8080:8080"]
      volumes:
        - ./config.yaml:/config/config.yaml:ro
        - ./ranking.yaml:/config/ranking.yaml:ro
        - ./cvs:/cvs:ro
        - ./data:/data
      healthcheck: { test: ["CMD", "curl", "-fsS", "http://localhost:8080/healthz"], interval: 60s }
  ```
  No `env_file`: the web process needs no secrets.
- Docs: `web` in CLAUDE.md commands and layout, a README section, tick off
  REMAINING_WORK §2 (and §4 tracking), PLAN.md §9 status.

## 5. Tests (`tests/test_web.py`, offline)

- Fixture builds a **file** DB in `tmp_path` (`:memory:` won't work: one
  connection per request), seeds jobs/rankings, writes a small ranking.yaml,
  `Config(data_dir=tmp_path, ...)`, `TestClient(create_app(config))`. Move
  `_job`/`_assessment` from `test_ranking.py` into `conftest.py` factories.
- Store: WAL on for file stores; read-only store rejects writes; GET / returns
  200 with old data while another connection holds an uncommitted
  `BEGIN IMMEDIATE` write.
- List: order follows `final_score`; each filter narrows; `""` params don't
  422; `HX-Request` returns a fragment (no `<html`); pending/excluded views and
  reasons; `/prefilter` counts match `matched_roles`.
- Detail: contacts with provenance labels, apply link, rationale, red flags,
  adjustment lines; unknown id → 404; old-version ranking → "will be re-ranked".
- Security: `<script>` in the description is escaped; `javascript:` URL not a
  link; POST without `HX-Request` → 403.
- Config reload: editing ranking.yaml weights changes the order without re-ranking.
- Budget: `record_llm_usage(cost_usd=1.23)` → widget shows "$1.23 of $20.00".
- Tracking: set/get state; ignored hidden by default; applied job visible in
  `view=tracked` after `expire_jobs`.
- `/static/htmx.min.js` → 200; `cmd_web` calls `uvicorn.run` with configured
  host/port (monkeypatched).
- `score_breakdown(...).total == final_score(...)`; `occupation_verdict` reasons.

## 6. Build order (each step green on `pytest` and `ruff`)

| # | Step | Size |
|---|---|---|
| 1 | Store: WAL + locked fallback, `readonly`/`check_same_thread`/`init_schema`; tests | S |
| 2 | Ranking helpers: `score_breakdown`, `occupation_verdict`, `prefilter_status`, `filter_fingerprint`, N+1 fix; optional per-role regex; tests | S–M |
| 3 | Web skeleton: deps, `WebConfig`, `create_app` + lifespan, `get_store`, base.html, vendored HTMX + CSS, `/healthz`, `cmd_web`; test GET / on empty DB | S–M |
| 4 | `views.load_rows` with prefilter memo; ranked list table; order tests | M |
| 5 | `ListFilters`, filter + sort, HTMX fragment, URL push; filter tests | M |
| 6 | Job detail page, `.json`; escaping + 404 tests | M |
| 7 | pending/excluded views, `/prefilter`; tests | M |
| 8 | Budget widget, `/status`, `Store.llm_usage_summary`; tests | S |
| 9 | Docker/compose, README, CLAUDE.md, REMAINING_WORK, PLAN.md | S |
| 10 | Tracking: table, model, buttons/notes, `view=tracked`, ignored hidden, HX-Request guard; tests | M |

Total ~1,200–1,600 lines (about a third templates, a third tests).
