# Jobsearcher: Project Plan (v0.4)

A personal pipeline that finds open positions, ranks them against my CV,
drafts tailored application documents, and presents everything in a web UI
running on my home server.

For what is still unfinished, see [REMAINING_WORK.md](REMAINING_WORK.md).

## Decisions so far

| Topic | Decision |
|---|---|
| LLMs | Configurable per role. Recommended: **Kimi K2.6 for ranking** (cheap, high volume) and **Claude via Claude Code on my Pro subscription for drafting** (few calls, best quality). The Claude API is also supported. |
| Budget | **$20 / month**, enforced in code |
| Hosting | **Local, in Docker on the home server.** GitHub hosts only the code. |
| Sources (v1) | **Platsbanken** (JobTech JobSearch API) + **JobTech Links**. LinkedIn/Indeed postponed. |
| CV input | One **master CV in Markdown** |
| Contact info | **Free sources only** |
| Application tracking | Later (DB table reserved, no UI yet) |
| Target roles | HR Business Partner, change management, operations manager, project manager (in `ranking.yaml`) |
| Locations | Stockholm, Gothenburg, Malmö, Lund, Uppsala (+ remote) |
| Language | English-language ads score higher; ads requiring Swedish score lower |
| Seniority | Not a priority |
| Server | Arch Linux, x86-64 (Intel), Docker |

---

## 1. Architecture

```
┌──────────────────────── docker compose (home server) ─────────────────────────┐
│                                                                               │
│  jobsearcher daemon (daily at 06:00)                                          │
│    1. SEARCH ──► normalise + dedupe ──► 2. RANK ──► 3. DRAFT                  │
│    (Platsbanken,  (Job schema,          (Kimi or     (Kimi or                 │
│     JobTech Links) SQLite)               Claude)      Claude)                 │
│                         │                                                     │
│                   data/jobsearcher.db  +  data/drafts/*.pdf|docx              │
│                         │                                                     │
│  jobsearcher web  ◄─────┘   4. WEB UI  (FastAPI, http://server:8080, LAN only)│
└───────────────────────────────────────────────────────────────────────────────┘
          5. COMPANY CRAWLER (later): company register → websites → career pages
```

Principles:
- **One normalised `Job` record** whatever the source. Every stage reads and
  writes the same SQLite store, and each stage can be re-run on its own.
- **Incremental and cached.** Rankings and drafts are keyed by a hash of
  (ad text, CV, prompt version, model), so nothing is paid for twice.
- **The LLM never invents experience.** Drafts are built from the master CV
  and checked against it.
- **Personal data stays home.** `config.yaml`, `cvs/` and `data/` are
  gitignored and mounted into the container as volumes.

Stack: Python 3.12, `httpx`, `pydantic`, SQLite, the `anthropic` SDK for Claude
and the `openai` SDK pointed at Moonshot for Kimi, FastAPI + Jinja/HTMX for the
UI, Docker Compose.

## 2. Module 1: Search ✅ (M1 implemented)

- `SourceAdapter.search(keyword) -> Iterator[Job]`, one adapter per source.
- **Platsbanken** (`jobsearch.api.jobtechdev.se/search`): full ad text,
  employer and org.nr, apply URL/email, and `application_contacts`
  (name, role, email, phone).
- **JobTech Links** (`links.api.jobtechdev.se/joblinks`): ads Arbetsförmedlingen
  collects from other Swedish job sites. Short brief and link to the original.
- Every run fetches **all currently open** matching ads, not just new ones.
  An ad that hasn't been seen for `expire_after_days`, or is past its deadline,
  is marked expired and its contacts are deleted. If any source fails, nothing
  is expired on that run.
- **Dedupe:** matched first by (source, source_id), then by a normalised
  company + title + city key (company suffixes such as "AB" and "(publ)" are
  stripped). Merged records keep every source link, the longest description
  and the union of contacts.
- Filters: exclude-keywords, location list, remote allowed.

**Contacts (free only), in order of preference:**
1. Structured `application_contacts` from Platsbanken ✅
2. Emails and phone numbers found in the ad text (regex) ✅; generic
   mailboxes (jobb@, hr@ …) are tagged as such.
3. Named people in the ad text ("Frågor om tjänsten besvaras av …"), extracted
   by the ranking LLM call at no extra cost (M2).
4. The company's own career/contact page (reuses crawler code, M7).
5. Fallback: a pre-built LinkedIn/Google search link for manual lookup.

Every contact records its `provenance`.

## 3. LLM layer ✅ (implemented)

`jobsearcher/llm/` hides the provider behind one call:
`complete(system, context, prompt, schema) -> LLMResult(text, parsed, usage)`.

| | Kimi (`moonshot`) | Claude API (`anthropic`) | Claude subscription (`claude_code`) |
|---|---|---|---|
| Billing | Per token | Per token (separate from Pro) | Claude Pro/Max usage limits; no per-token cost |
| Suggested ranking model | `kimi-k2.6` | `claude-haiku-4-5` | not recommended: volume would eat the usage limits |
| Suggested drafting model | `kimi-k3` | `claude-opus-5-5` (with `effort`) | `opus` (with `effort`) |
| Structured output | JSON mode + schema in the prompt, validated with pydantic, one retry | Native `output_config.format` JSON schema | `claude -p --output-format json --json-schema` |
| CV caching | Automatic prefix caching | Explicit `cache_control` breakpoint after the CV | Handled by Claude Code |
| Refusals | n/a | Server-side refusal fallback on Opus 5.5 / Sonnet 5.5; refusals raise `LLMRefusal` | Errors, including "usage limit reached", raise `LLMError` |
| Credentials (`.env`) | `MOONSHOT_API_KEY` | `ANTHROPIC_API_KEY` | `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` |

**About `claude_code`:** the pipeline runs the official Claude Code CLI in
non-interactive mode (`claude -p`), with no tools, from an empty working
directory. It doesn't use `--bare`, because bare mode ignores subscription
logins. The subscription token is only ever used by Claude Code itself, never
by our own code: Anthropic doesn't allow subscription logins in third-party
clients. Calls share the Pro allowance with normal Claude use (it resets every
5 hours, plus a weekly cap). The Docker image installs the CLI; build with
`--build-arg INSTALL_CLAUDE_CODE=false` to skip it. Automated daily use of a
consumer plan is a grey area, so check Anthropic's current consumer terms.

Every pay-per-token call goes through `BudgetedLLM`: it checks month-to-date spend first and
writes the tokens and cost to `llm_usage` afterwards. Failed calls that were
still billed (refusals, truncation) are recorded too. `jobsearcher llm-check`
sends one tiny request to each configured model; `jobsearcher budget` shows
spend so far this month.

## 4. Module 2: Ranking ✅ (implemented; needs a first live run)

Configured in **`ranking.yaml`** (copy from `ranking.example.yaml`):
- `target_roles`: each has a name and aliases in Swedish and English. By
  default they are also the search keywords (`search.use_target_roles`).
- `preferences`: seniority, languages, likes, dislikes and dealbreakers,
  given to the model as a brief.
- `weights`: how `fit` and `success` combine into the ranking score.
- `adjustments`: points added for facts the model reads from the ad: +10
  if the ad is in English, −20 if Swedish is required, −5 if Swedish is only a
  merit. These are applied when scores are displayed, so tuning them (or the
  weights) never costs a re-rank.
- `prefilter`: whether a job must mention a target role, and the maximum
  number of LLM calls per run.
- `drafting`: minimum score and maximum drafts per day (used in M4).

Flow (`jobsearcher rank`, and daily as part of `jobsearcher run`):
1. **Prefilter without an LLM:** a job must mention a target role name or alias
   as a whole word, in the title or the ad text. Title matches and newer ads
   go first.
2. **One LLM call per job.** The prompt holds the instructions, then the roles,
   preferences and master CV (the stable, cacheable part), then the ad. The
   answer is structured JSON: `fit_score`, `success_score`, matched role,
   matched and missing requirements, red flags, a rationale, the ad language,
   and **contact persons named in the ad**. Those contacts are merged into the
   job with provenance `llm:ad_text`, and the prompt forbids invented contacts.
3. **Caching:** a ranking is keyed by a hash of (ad text, CV, roles and
   preferences, model, prompt version). Re-runs cost nothing. Editing the CV
   or the roles/preferences re-ranks everything once; changing limits,
   weights or adjustments does not.
4. **Limits:** the per-run cap and the monthly budget both stop ranking
   cleanly. Jobs left over are ranked on the next run, and failed calls are
   retried then too.

Still to do: a `calibrate` command. You hand-score about 20 jobs and it
reports how well the model's ranking agrees.

## 5. Module 3: Drafting (stronger model)

- Only for jobs above a score threshold, capped at N per day.
- Input: `cvs/master.md`, the ad, and the ranking output.
- Output: a tailored CV in Markdown and a cover letter in the ad's language
  (Swedish or English), addressed to the named contact when known.
- **Grounding check:** a second, cheap call compares the draft against the
  master CV and flags any claim not supported by it. Flagged drafts are
  marked for review in the UI.
- Rendering: Markdown → **PDF** (Typst or WeasyPrint template) and **DOCX**
  (pandoc) for hand editing.

## 6. Module 4: Web UI (local)

Served by a FastAPI container on the LAN (`http://<server>:8080`). For
access from outside, use Tailscale or WireGuard rather than exposing a port.

- Ranked list with filters (score, source, location, remote, deadline).
- Job page: ad text, scores and rationale, matched/missing requirements,
  contacts with provenance, **Apply** link, downloads for the tailored CV and
  cover letter, and a **Regenerate** button.
- Budget widget: spend this month vs. $20.
- Later: application tracking (applied / interview / rejected, notes).

## 7. Budget ($20 / month)

Rough estimates at list prices (Sep 2026). Assumes ~1,500 ranked jobs and ~150
drafts a month, with a CV of about 3k tokens.

| Setup | Ranking | Drafting | Total / month |
|---|---|---|---|
| **All Kimi** (K2.6 + K3) | ~$5 (≈$3 via Batch) | ~$8–10 | **~$13–15** |
| **All Claude** (Haiku 4.5 + Opus 5.5) | ~$10 (≈$5 via Batch) | ~$15 | **~$20–25**: over budget at this volume |
| **Mixed** (Kimi K2.6 ranking + Opus 5.5 API drafting) | ~$5 | ~$15 | **~$20** |
| **Recommended** (Kimi K2.6 ranking + Claude Code drafting on Pro) | ~$5 | Pro usage limits | **~$5** plus the existing Pro subscription |

Notes on Claude:
- Haiku 4.5 caches only prompts of at least 4,096 tokens, so a ~3k-token CV
  won't be cached and every ranking call pays full input price.
- Opus 5.5 always thinks before answering. `effort` (default `medium`)
  controls how much, and those thinking tokens are billed as output.
- Staying under $20 with Claude drafting means capping drafts per day,
  ranking in batches, or choosing a cheaper drafting model.

Enforcement: drafting pauses at 80% of the monthly budget
(`drafting_budget_share`) and ranking at 100%. Prices are built in for the
models above and can be overridden in `config.yaml`. A model without a known
price is costed at the highest known rate, so an unpriced model can't slip
past the budget.

## 8. Module 5: Company crawler (later)

1. Company list from **Bolagsverket open data / SCB företagsregister**
   (filtered by SNI code, size and region) rather than scraping allabolag.
2. Website discovery, verified by org.nr/name on the site.
3. Career-page detection (`/karriar`, `/jobb`, `/lediga-tjanster`, `/careers`,
   links to known applicant-tracking systems).
4. One adapter per applicant-tracking system (Teamtailor, Varbi, ReachMee,
   Jobylon, Workable, Greenhouse, Lever …), many of which offer structured feeds.
5. Generic fallback: LLM extraction from the career page HTML (counts
   against the budget, so run it weekly and only on changed pages).
6. Polite crawling: `robots.txt`, rate limits per domain, caching.

## 9. Milestones

| # | Milestone | Status |
|---|---|---|
| M0 | Skeleton: package, config, `Job` schema, SQLite store, CLI, Docker, tests | ✅ |
| M1 | Platsbanken + JobTech Links search with contacts, dedupe and expiry | ✅ (needs first live run) |
| M2a | LLM layer: Kimi + Claude providers, budget tracking and enforcement | ✅ |
| M2 | Ranking: `ranking.yaml`, prefilter, LLM scoring, contacts from ad text | ✅ (calibration command still to do) |
| M3 | Local web UI (list, detail, apply link, contacts) | next |
| M4 | Drafting: tailored CV + cover letter, grounding check, PDF/DOCX | |
| M5 | Contacts from company sites, application tracking | |
| M6 | Company crawler | |
| M7 | Optional: LinkedIn/Indeed adapters | |

## Open questions

1. **Likes, dislikes and dealbreakers** in `ranking.yaml` are still
   placeholders.
2. **Master CV** at `cvs/master.md` on the server (gitignored).
3. **Credentials** in `.env`: `MOONSHOT_API_KEY` for ranking and
   `CLAUDE_CODE_OAUTH_TOKEN` for drafting. Then run `jobsearcher llm-check`.
