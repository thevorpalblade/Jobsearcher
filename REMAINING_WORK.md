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
| M2a LLM layer: Kimi, Claude API, Claude Code (subscription), budget | Done; only one real Claude Code call made, no real Kimi or Anthropic API call |
| M2 Ranking (`ranking.yaml`, prefilter, LLM scoring) | Done, **never run on real ads**; `calibrate` command missing |
| M3 Local web UI | **Not started** (next) |
| M4 Drafting (tailored CV + cover letter, PDF/DOCX) | **Not started** |
| M5 Contacts from company sites, application tracking | **Not started** |
| M6 Company crawler (Bolagsverket → websites → career pages) | **Not started** |
| M7 LinkedIn / Indeed adapters | **Not started** (optional; terms-of-service risk) |

Tests: 40 passing (`pytest`), lint clean (`ruff check .`). All tests run
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
- [ ] `jobsearcher llm-check` with real `MOONSHOT_API_KEY` and
      `CLAUDE_CODE_OAUTH_TOKEN`. Confirm Moonshot reports cached tokens where
      `_cached_tokens()` in `llm/moonshot_client.py` expects them, and that the
      model IDs `kimi-k2.6` / `kimi-k3` and their prices in `config.py`
      `DEFAULT_PRICES` are still current.
- [ ] First `jobsearcher rank` on real ads. Sanity-check the scores and the
      `swedish` / `language` fields the language adjustments depend on.

## 1. Ranking leftovers (M2)

- [ ] **`jobsearcher calibrate`**: the user hand-scores ~20 jobs (e.g. a CSV of
      `job_id,score`). The command reports rank correlation (Spearman) against
      `final_score` and lists the biggest disagreements, to guide prompt and
      `ranking.yaml` tuning.
- [ ] Optional: **Moonshot Batch API** for overnight ranking (~40% cheaper,
      per PLAN.md §7). Only worth it if ranking spend gets near the budget.
- [ ] Optional: a CV-similarity prefilter (keyword or embedding overlap). Today
      the only prefilter is a whole-word match on target role names/aliases
      (`ranking/prefilter.py`).

## 2. Web UI (M3), next up

PLAN.md §6. Not started: there is no FastAPI dependency or web service yet.

- [ ] FastAPI + Jinja/HTMX app (`jobsearcher web`), served on port 8080 by a
      second service in `docker-compose.yml` sharing the `data/` volume. LAN
      only; remote access via Tailscale or WireGuard, never a public port.
- [ ] Ranked list with filters (score, source, location, remote, deadline),
      using `ranking.ranked_jobs(store, config)`, which already returns
      `(job, ranking, final_score)` best first.
- [ ] Job page: ad text, scores, rationale, matched/missing requirements, red
      flags, contacts **with provenance**, Apply link, all source links.
- [ ] Budget widget (`BudgetTracker.month_to_date()` and `subscription_usage()`).
- [ ] Later (after M4): draft downloads and a Regenerate button.

## 3. Drafting (M4)

PLAN.md §5. Not started apart from the empty `drafts` table in
`store.py` and the thresholds in `ranking.yaml` (`drafting.min_score`,
`drafting.max_drafts_per_day`).

- [ ] `jobsearcher draft`: for open jobs with `final_score >= min_score`, best
      first, at most `max_drafts_per_day` per day. Use `make_llm(config, "drafting", ...)`;
      the recommended provider is `claude_code` (subscription), which isn't
      gated by the dollar budget, so the daily cap is what protects the user's
      Pro usage limits.
- [ ] Input: `cvs/master.md`, the ad, and the ranking. Output: a tailored CV
      (Markdown) and a cover letter in the ad's language, addressed to a named
      contact when there is one.
- [ ] **Hard rule: never invent experience.** Add a grounding check (a second,
      cheap call) that flags any claim not supported by the master CV. Flagged
      drafts are marked for review, not silently shipped.
- [ ] Cache drafts by a hash of (ad, CV, ranking, model, prompt version), as
      ranking does.
- [ ] Rendering: Markdown → PDF (Typst or WeasyPrint) and DOCX (pandoc), into
      `data/drafts/`. The toolchain must be installed in the Docker image
      (Debian-based `python:3.12-slim`; the server is Arch on x86-64).
- [ ] Add drafting to `cmd_run` / the daily daemon after ranking.

## 4. Contacts and tracking (M5)

- [ ] Fallback contact: a pre-built LinkedIn/Google search link (company + role)
      when no contact person is known (PLAN.md §2, item 5).
- [ ] Contacts from the company's own career/contact page (shares code with M6).
- [ ] Application tracking: states new / shortlisted / applied / interview /
      rejected / ignored, plus notes. **There is no `applications` table yet**;
      add one in `store.py` and expose it in the web UI.

## 5. Company crawler (M6)

PLAN.md §8. Company list from Bolagsverket open data or SCB (not scraping
allabolag) → website discovery verified by org.nr → career-page detection →
one adapter per applicant-tracking system (Teamtailor, Varbi, ReachMee,
Jobylon, Workable, Greenhouse, Lever …) → LLM extraction as a fallback.
Respect `robots.txt`, rate-limit per domain, and re-crawl weekly.

## 6. LinkedIn / Indeed (M7, optional)

Postponed by the user. No official API exists; `python-jobspy` scrapes them,
which breaks LinkedIn's terms and is brittle. Low volume and no logged-in
scraping, if done at all.

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
