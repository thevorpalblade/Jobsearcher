# Remaining work

Handoff notes for whoever picks this project up next (human or Claude). Read
[PLAN.md](PLAN.md) for the full design and the decisions already made; this
file only lists what is **not done yet**, in suggested order. Last updated
2026-10-01.

## Status at a glance

| Milestone | State |
|---|---|
| M0 Skeleton (package, config, SQLite store, CLI, Docker, tests) | Done |
| M1 Search: Platsbanken + JobTech Links, dedupe, contacts, expiry | Done; first live run 2026-10-01 (outside Docker) |
| M2a LLM layer: Kimi, NVIDIA (e.g. GLM), Claude API, Claude Code (subscription), budget | Done; Claude Code and NVIDIA GLM tested live, no real Kimi or Anthropic API call |
| M2 Ranking (`ranking.yaml`, prefilter, LLM scoring) | Done; all ~350 candidates ranked live with GLM on 2026-10-01; `calibrate` command missing |
| M3 Local web UI | Done (`jobsearcher web`, [docs/m3-web-ui.md](docs/m3-web-ui.md)); smoke-tested on a copy of the live DB, not yet run in Docker |
| M4 Drafting (tailored CV + cover letter, PDF/DOCX) | Done 2026-10-05: on demand, checked against the CVs (docs/m4-drafting.md) |
| M5 Contacts from company sites (application tracking was done in M3) | **Not started** |
| M6 Target companies: ATS crawling + news signals (docs/m6-companies.md) | Done (phases 1–3); first live runs 2026-10-01 |
| M9 Landing dashboard + chat with Claude Code (docs/m9-dashboard-chat.md) | Done 2026-10-05 |
| M7/M8 More sources: LinkedIn + Indeed (JobSpy), Workday, JSON-LD, recruiters, Chrome crawling (docs/m8-more-sources.md) | Done 2026-10-05 |

Tests: 137 passing (`pytest`), lint clean (`ruff check .`). All tests run
offline against recorded or simulated responses.

## 0. Verify what exists (do this first, on the real server)

Nothing below has touched a live system. The development sandbox could not
reach `jobsearch.api.jobtechdev.se` or `links.api.jobtechdev.se`, and Docker
could not be built there.

- [ ] `docker compose build` on the Arch Linux server. The Dockerfile runs the
      Claude Code native installer (`curl -fsSL https://claude.ai/install.sh | bash`)
      as the `app` user. Confirm it succeeds and `claude --version` works in the image.
- [x] `jobsearcher search` against the real APIs (2026-10-01, outside Docker):
      16 keywords, ~1,400 ads fetched, 714 open jobs kept after the location filter.
      Field mappings checked for both sources. JobTech Links carries no deadline,
      org.nr or employment type, and ~95% of its hits only link back to Platsbanken
      ads, so those are now skipped while Platsbanken is enabled.
- [ ] Optional: replace `tests/fixtures/*.json` with real (anonymised) responses.
- [x] `claude_code` provider: `llm-check` succeeded for Haiku 4.5 and Opus 5.5
      (2026-10-01).
- [ ] Kimi isn't in use (ranking runs on GLM via NVIDIA). If it's switched on:
      `jobsearcher llm-check` with a real `MOONSHOT_API_KEY`. Confirm Moonshot reports cached tokens where
      `_cached_tokens()` in `llm/openai_compatible.py` expects them, and that the
      model IDs `kimi-k2.6` / `kimi-k3` and their prices in `config.py`
      `DEFAULT_PRICES` are still current.
- [x] First `jobsearcher rank` on real ads (2026-10-01; re-ranked after the CV
      and preference changes). Scores looked sensible; language fields work.

## 1. Ranking leftovers (M2)

- [ ] **`jobsearcher calibrate`**: the user hand-scores ~20 jobs (e.g. a CSV of
      `job_id,score`). The command reports rank correlation (Spearman) against
      `final_score` and lists the biggest disagreements, to guide prompt and
      `ranking.yaml` tuning.
- [x] **Parallel ranking** (`llm.ranking.max_parallel`) and server-enforced JSON
      schemas (`enforce_schema`) for OpenAI-compatible providers. On NVIDIA's free
      tier GLM 5.3 Flash calls are queued (85–330 s each); with plain JSON mode GLM
      sometimes echoed the schema back instead of filling it in.
- [ ] Optional: longer retries with backoff on 429/504. Today the OpenAI SDK
      retries twice, and jobs that still fail are retried on the next run.
- [ ] Optional: **Moonshot Batch API** for overnight ranking (~40% cheaper,
      per PLAN.md §7). Only worth it if ranking spend gets near the budget.
- [ ] **Text-only role matches.** ~180 candidates mention a target role only in
      the ad text, not the title (mostly "förändringsledning" for Change
      management). Consider ranking title matches first and capping body-only ones.
- [ ] Optional: a CV-similarity prefilter (keyword or embedding overlap). Today
      the prefilter is a whole-word match on target role names/aliases plus
      per-role occupation filters (`exclude_occupations`, `except_occupations`,
      `include_occupations` in `ranking.yaml`; `jobsearcher occupations --groups`
      lists the labels). "Projektledare" is mostly construction/engineering in
      Sweden, which is what the occupation filters are for.

## 2. Web UI (M3)

PLAN.md §6; implementation notes and decisions in
[docs/m3-web-ui.md](docs/m3-web-ui.md).

- [x] FastAPI + Jinja/HTMX app (`jobsearcher web`), served on port 8080 by the
      `web` service in `docker-compose.yml` sharing the `data/` volume (SQLite in
      WAL mode, a read-only connection per request). LAN only; set `WEB_BIND`
      on a server with a public IP.
- [x] Job list: all ranked jobs by final score, with filters (view, score,
      source, location, remote, deadline, role, occupation, language, Swedish
      requirement, contact, first seen, search) and sorting; HTMX updates the
      table and the URL, and it also works without JavaScript.
- [x] Job page: ad text, score breakdown, rationale, matched/missing
      requirements, red flags, a stale badge, contacts **with provenance**,
      Apply link, all source links, prefilter diagnostics; `/jobs/<id>.json`.
- [x] Pending/excluded views and `/prefilter` (the web `occupations --groups`).
- [x] Budget widget in the header and `/status` (last runs, counts, spend by model).
- [x] Running outside Docker as systemd user services (`deploy/install-systemd.sh`, 2026-10-05):
      start at boot, restart on crash, journald logs.
- [ ] **Run it in Docker** on the server (optional now) (`docker compose up -d --build`), check
      that compose builds `jobsearcher:local` once for both services and that
      the healthcheck passes.
- [ ] Later (after M4): draft downloads and a Regenerate button. Regenerate
      should queue a request for the daemon, so the web container never needs
      API keys or the Claude token.

## 2a. Dashboard and chat (M9, done 2026-10-05)

`/` is a dashboard (greeting from `web.user_name`, top five jobs she hasn't acted
on, counts) with a chat panel that runs Claude Code in this checkout
(`chat.enabled`, off by default). The job list moved to `/jobs`.

- [ ] **Security:** the user chose no login and full access, so anyone who can open
      the page can run Claude Code on this machine. Mitigations: off by default, the
      HX-Request header on writes, a Host-header check against DNS rebinding
      (`web.allowed_hosts`). A shared password (`WEB_PASSWORD`) would be the next step.
- [ ] The chat needs the `claude` CLI and the checkout, so it doesn't work in the
      Docker web service.
- [ ] Replies appear per message, not token by token (`--include-partial-messages`
      would stream words); the chat shows tool activity as one-line notes.
- [ ] One run at a time across the whole UI (shared subscription limits, one checkout).

## 2b. Settings page (done 2026-10-04)

`/settings` in the web UI: upload reference CVs (PDF/DOCX/Markdown → Markdown,
reviewed and edited in the browser), pick the master CV (its text is copied
into `cv_path`, old master backed up), and edit `config.yaml`, `ranking.yaml`
and `companies.yaml` as YAML with validation, change effects ("re-ranks
everything") and backups in `data/backups/`.

- [ ] Drafting (M4) should read the reference CVs (`cvs/*.md` other than the master)
      as extra facts, still grounded: nothing outside the CVs may be claimed.
- [ ] No login: the page writes personal files, so keep the UI on the LAN/Tailscale.
- [ ] PDF conversion is plain text (no headings); scanned PDFs need OCR first.

## 3. Drafting (M4): built 2026-10-05, on demand

See [docs/m4-drafting.md](docs/m4-drafting.md). Draft from the job page, the dashboard
or `jobsearcher draft`; shortlisting a job starts one (`drafting.auto_on_shortlist`, capped
by `max_drafts_per_day`); spontaneous applications from news signals (`/companies/<slug>`).
English only. Claude Code writes, GLM checks every claim against the CVs, one repair round,
a model-free check of figures; anything unsupported marks the draft "needs review".
Word via python-docx, PDF via LibreOffice, a Markdown preview in the browser.

- [ ] Editing a draft in the browser (today: edit the downloaded Word file).
- [ ] The grounding check is only as strict as GLM; a stricter model (Claude Haiku) is a
      config switch away if drafts slip through.
- [ ] Contacts addressed by name come from the ad or the ranking model (`llm:ad_text`);
      check the name before sending.
- [ ] PDF needs LibreOffice (not in the Docker image; the drafts also work in Docker
      without it, as Word + Markdown).
- [ ] A cover-letter template with her own letterhead / CV styling beyond the plain layout.

## 4. Contacts and tracking (M5)

- [ ] Fallback contact: a pre-built LinkedIn/Google search link (company + role)
      when no contact person is known (PLAN.md §2, item 5).
- [ ] Contacts from the company's own career/contact page (shares code with M6).
- [x] Application tracking (built with M3): states new / shortlisted / applied /
      interview / rejected / ignored plus notes, in the `applications` table, set
      from the job page. Ignored jobs are hidden from the list; `view=tracked`
      shows every tracked job, expired ones included.
- [ ] Optional: reminders or a follow-up date per tracked application.

## 5. Target companies (M6)

Built per [docs/m6-companies.md](docs/m6-companies.md): `companies.yaml` (seed:
`companies.example.yaml`, 107 employers with sources), ATS detection cached in
`company_ats`, adapters for Teamtailor, Varbi, Lever, Greenhouse and
SmartRecruiters, per-source expiry, and weekly news signals from GDELT
classified by the ranking LLM (`jobsearcher signals`).

Live results (2026-10-01): a supported ATS for 31 of 107 companies; the company
feeds added ~1,900 open jobs (mostly Region Stockholm/VGR healthcare) and ~175
new ranking candidates.

- [x] Workday adapter, Chrome impersonation for the sites that blocked us, and
      Google News for signals: done in M8 (§6).
- [x] **ReachMee** and **Jobylon** adapters (2026-10-05).
- [ ] Companies with no ATS and no JSON-LD: set `careers_url` or a manual `ats:`
      in `companies.yaml`, or add LLM extraction from careers pages.
- [ ] News: MFN press releases (listed companies' M&A and reorganisations) as a
      second signal source.
- [ ] ATS jobs have no occupation labels, so the per-role occupation filters
      don't apply to them; the role prefilter and the LLM decide.
- [ ] Web UI: a Companies page with the signals digest (after M3 is merged), and
      in M4 a spontaneous-application draft for a chosen company.

## 6. More sources (M8, docs/m8-more-sources.md): done 2026-10-05

Crawl settings (`crawl.user_agent: chrome` impersonates Chrome via curl_cffi;
`crawl.respect_robots`), Google News for signals, 12 recruitment agencies,
LinkedIn + Indeed via JobSpy, a Workday adapter, and a generic JSON-LD reader.

- Headless browser: dropped (user decision, 2026-10-05). JavaScript-only careers
  pages (Academic Work and other recruiters) are covered via LinkedIn/Indeed/Platsbanken.
- [x] **SuccessFactors** (career sites' RSS: Volvo Cars, Scania, Axfood, Atlas Copco),
      **ReachMee** (Sweco, Bravida, Regeringskansliet, Göteborgs universitet, Malmö stad)
      and **Jobylon** (Coor, Unilabs, Kronans Apotek, LKAB) adapters (2026-10-05).
      Ericsson moved to Eightfold (jobs.ericsson.com): not supported; its jobs reach
      LinkedIn.
- [ ] JobSpy: watch for LinkedIn rate limiting (429) as volume grows; Glassdoor
      and Google Jobs are supported but untested.
- [ ] Dedupe can't merge the same ad under different titles across sources
      (e.g. "HR Business Partner" vs "Human Resources Business Partner").

## Known caveats / tech debt

- **Dedupe can over-merge:** two genuinely different ads with the same normalised
  (company, title, city) collapse into one job (`models.dedupe_key`).
- **Location filter is a substring match** (`pipeline.matches_filters`):
  "Stockholm" also matches all of Stockholms län, which is intended, but short
  names like "Lund" could match other places.
- **Claude refusal fallback cost** is approximated: the whole call is priced
  at the model that finished it (`llm/anthropic_client.py`).
- **Haiku 4.5 won't cache** a CV shorter than 4,096 tokens. This only matters
  if ranking is switched to the Anthropic provider.
- **Prices** in `config.DEFAULT_PRICES` are list prices from Sep 2026. Re-check
  them before relying on budget numbers.
- **`claude_code` provider:** automated daily use of a consumer Claude
  subscription is a grey area under Anthropic's consumer terms. It must keep
  running the official `claude` CLI; never pass the subscription token to the
  SDK or any other client. Don't add `--bare`, which ignores subscription logins.
- **Placeholders the user still has to fill in:** likes, dislikes and
  dealbreakers in `ranking.yaml`; `exclude_keywords` in `config.yaml`; the
  master CV at `cvs/master.md`; and credentials in `.env`.
- **Repository housekeeping (user action):** `main` is a single squashed commit.
  The old branch `claude/job-application-tool-plan-g3lxen` still carries ~30 MB
  of accidentally committed `.whl` files in its history. Make `main` the
  default branch on GitHub and delete the old branch to drop them.
