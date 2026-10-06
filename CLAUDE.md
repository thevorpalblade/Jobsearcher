# Jobsearcher: notes for Claude

A personal job-search pipeline: search Swedish job boards, rank ads against
the user's master CV with an LLM, draft tailored applications, and show them
in a local web UI. It runs in Docker on the user's Arch Linux home server.

- **Design and decisions:** [PLAN.md](PLAN.md). Don't re-open settled decisions
  (local hosting, free contact sources only, $20/month budget, Kimi for ranking
  and Claude Code for drafting) without the user.
- **What's left to do:** [REMAINING_WORK.md](REMAINING_WORK.md). Read it before
  starting work, and update it when you finish something.

## Commands

```sh
pip install -e '.[dev]'
pytest                 # all tests run offline; keep it that way
ruff check . && ruff format --check .
jobsearcher draft <job id> [--instructions "..."] [--cv NAME]   # or --company NAME; jobsearcher drafts
deploy/install-systemd.sh   # run the daemon + web UI as systemd user services (restart after code changes)
jobsearcher --help     # search | rank | run | list | show | occupations | companies | signals | llm-check | budget | daemon | web
jobsearcher web        # local web UI on http://127.0.0.1:8080: dashboard (/), jobs (/jobs), chat with Claude Code
```

## Layout

- `jobsearcher/sources/`: one adapter per job board, returning normalised `models.Job`
- `jobsearcher/store.py`: SQLite store (jobs, job_sources, rankings, drafts, llm_usage,
  applications); WAL mode, so the web UI can read while the daemon writes
- `jobsearcher/llm/`: provider-neutral `complete(system, context, prompt, schema)`.
  Every call goes through `BudgetedLLM`, which records cost and enforces the budget.
- `jobsearcher/ranking/`: `ranking.yaml` config, prefilter, scoring, `final_score`
- `jobsearcher/pipeline.py`: the search stage (job boards, then company feeds). `cli.py` wires the stages together.
- `jobsearcher/web/`: FastAPI + Jinja + HTMX UI (`docs/m3-web-ui.md`). `views.py`
  shapes data with no FastAPI; `app.py` has the routes. Routes are sync `def`s with a
  read-only `Store` per request (`get_store`); the web process needs no secrets and
  never calls an LLM. HTMX is vendored in `web/static/` (no CDN). `/settings` edits
  CVs (`cvs.py`: uploads converted to Markdown, master + reference CVs) and the
  YAML configs (`settings.py`: validated, backed up to `data/backups/`, written in
  place for Docker bind mounts). Ranking and companies have real forms
  (`web/forms.py`) whose values are merged into the YAML with ruamel
  (`settings.merge_yaml`), so comments and formatting survive; config.yaml and an
  "Edit as YAML" fallback use a text editor. `.env` is never shown or edited.
- `jobsearcher/drafting/`: on-demand application drafts (`docs/m4-drafting.md`). `service.py` builds a job's
  or company's request and runs `core.generate_draft`: Claude Code writes a tailored CV + cover letter
  (English, JSON schema), GLM checks every claim against the CVs, one repair round, plus a model-free check
  of figures. `render.py` makes Word (python-docx) and PDF (LibreOffice); `manager.py` runs web-triggered
  drafts one at a time. Shortlisting a job starts one (`drafting.auto_on_shortlist`). Never invent experience.
- `jobsearcher/chat.py`: the dashboard's chat; runs headless `claude -p` (stream-json, resumed per
  conversation) in this checkout with full permissions, one run at a time, off unless `chat.enabled`.
  The UI has no login, so chat routes need the HX-Request header and a non-public Host header.
- `jobsearcher/companies/`: target companies (`companies.yaml`), ATS detection, polite crawling;
  `jobsearcher/sources/ats/`: one adapter per ATS feed (Teamtailor, Varbi, Lever, Greenhouse,
  SmartRecruiters, Workday, SuccessFactors, ReachMee, Jobylon) plus `jsonld.py`, a generic
  reader for job ads with schema.org JobPosting data
- `jobsearcher/places.py`: municipality -> county, so the location filter treats every source
  like Platsbanken (which gives the county)
- `jobsearcher/sources/jobspy_source.py`: LinkedIn/Indeed via JobSpy (optional `jobspy` extra);
  these jobs expire by age, not by absence
- `benchmarks/ranking/` + `scripts/rank_benchmark.py`: a fixed job set and every ranking model's
  scores on it, to compare a new model before switching (scores only: no CV-derived text in git)
- `jobsearcher/signals/`: company news from GDELT, classified by the LLM into spontaneous-application signals

## Rules

- **Personal data never goes into git:** `config.yaml`, `ranking.yaml`, `companies.yaml`,
  `.env`, `cvs/`, `data/` are gitignored. Only `*.example.*` files are committed.
- **The LLM must never invent experience or contacts.** Drafts are grounded in
  `cvs/master.md`, and contacts always carry a `provenance`.
- **Avoid paying twice:** cache LLM results by a hash of their inputs, as
  `ranking/ranker.py:input_hash` does, and bump `PROMPT_VERSION` when a prompt
  or schema changes.
- **`claude_code` provider:** only ever run the official `claude` CLI with the
  subscription token. Never hand that token to an SDK. Don't use `--bare`.
- The JobTech APIs may be unreachable from cloud sandboxes, so test against
  fixtures in `tests/fixtures/`.
- **Crawl through `PoliteClient`** (`companies/http.py`): per-host pacing always, plus
  the user's `crawl` settings (user agent; robots.txt on or off). The user runs this
  for one person at low volume and has chosen a Chrome user agent and no robots.txt;
  keep the pacing, and keep the code defaults polite for other setups.
- Match the surrounding style: type hints, pydantic models, short comments
  that explain why.
- **Web UI:** ad text, titles and URLs are untrusted. Keep Jinja autoescaping on,
  only turn `http(s)` URLs into links (`views.safe_url`), and require the
  `HX-Request` header on every POST (a cheap CSRF guard, since there's no login).
