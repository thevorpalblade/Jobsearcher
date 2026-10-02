# M6: target companies, ATS crawling and news signals

Approved by the user on 2026-10-01. **Built 2026-10-01** (phases 1–3); open
items are in REMAINING_WORK.md §5. Goal: find jobs that never reach
Platsbanken or JobTech Links (many employers only post on their own careers
site), and spot companies worth a spontaneous application (spontanansökan)
from what's happening there.

## Decisions

1. Seed list: the ~100 largest employers **by employees in Sweden** (not global
   revenue), plus groups that fit the candidate: private healthcare operators,
   international companies with Stockholm offices. The user can add any company.
2. ATS jobs go through the existing location filter: listed locations plus
   remote, and jobs outside Sweden are dropped.
3. Plan first (this file), then build phase 1 → 2 → 3.

## Verified ATS feeds (live requests, 2026-10-01)

| ATS | Public feed | Status |
|---|---|---|
| Teamtailor | `<careers site>/jobs.json` (JSON Feed with schema.org `JobPosting`, incl. location) and `/jobs.rss` | open |
| Varbi | `https://<customer>.varbi.com/en/what:rssfeed/` | open (RSS) |
| Lever | `https://api.lever.co/v0/postings/<company>?mode=json` | open |
| SmartRecruiters | `https://api.smartrecruiters.com/v1/companies/<id>/postings` | open, global: filter to Sweden |
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true` | open; board names must be detected, not guessed |
| Workable | `https://apply.workable.com/api/v1/widget/accounts/<account>` | to confirm with a real account |
| Workday | career site's `/wday/cxs/<tenant>/<site>/jobs` (POST) | **422 on every guess**; used by many large employers, needs investigation |
| Google News | `https://news.google.com/rss/search?q=...` | **disallowed by robots.txt** (`Disallow: /`); not used |
| GDELT DOC 2.0 | `https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=artlist&format=json&timespan=30d` | open API; ≤1 request / 5 s, strictly enforced; used for news |

## Data model

- **`companies.yaml`** (user-maintained, gitignored; `companies.example.yaml`
  committed with the seed list): per company `name`, `website`, optional
  `org_nr`, `careers_url`, `tags` (e.g. `largest`, `healthcare`,
  `international`), `news_query` (defaults to the quoted name), and an optional
  manual `ats: {type, ref}` override when detection gets it wrong.
- **Discovered state lives in SQLite**, not the YAML:
  - `company_ats(company, ats_type, ats_ref, careers_url, checked_at, error)`:
    detection results, re-checked weekly.
  - `news_items(id, company, title, url, source, published_at, fetched_at)`.
  - `signals(item_id, company, kind, relevance, summary, model, created_at)`.
- Config: `config.yaml` gets `companies_config: companies.yaml` and
  `sources.companies: true`.

## Phase 1: company list

- Compile the seed list from public sources (largest employers in Sweden by
  number of employees; healthcare operators; international employers in
  Stockholm) with websites. Org.nr where easily available. Every entry must
  come from a source, never invented.
- `jobsearcher/companies/config.py`: pydantic models + `load_companies()`.
- `jobsearcher companies` CLI: list companies with their detected ATS,
  last check, job count, and errors.

## Phase 2: ATS detection and adapters

- `jobsearcher/companies/detect.py`: fetch the website (or `careers_url`), find
  the careers page (links containing karriar, karriär, jobb, jobba, careers,
  jobs, lediga-tjanster, work-with-us …), and match known ATS URL patterns
  (`*.teamtailor.com` or a Teamtailor-powered custom domain, `*.varbi.com`,
  `jobs.lever.co/<x>`, `boards.greenhouse.io/<x>` / `job-boards.greenhouse.io`,
  `jobs.smartrecruiters.com/<x>`, `apply.workable.com/<x>`,
  `*.myworkdayjobs.com`). One level of link following; polite crawling:
  `robots.txt`, the existing User-Agent, ≥1 s between requests per domain.
- `jobsearcher/sources/ats/`: one module per ATS with
  `fetch_jobs(ref, company) -> Iterator[Job]`, normalised to `Job`
  (description as text, location/country, apply URL, published date), source
  name `"<ats>:<company slug>"`. Drop jobs outside Sweden.
- Pipeline: `run_search` runs keyword sources as today, then each company with
  a known ATS once (no keyword). Same filters, dedupe and upsert, so ranking,
  occupation filters and the web UI work unchanged. (ATS jobs have no
  occupation labels, so they pass occupation filters; the role prefilter and
  the LLM decide.)
- Expiry becomes per source: `expire_jobs(..., skip_sources=failed)` skips jobs
  with any source that failed this run, instead of skipping expiry entirely
  when anything failed (with ~150 companies, something will always fail).
- Order: Teamtailor, Varbi, SmartRecruiters, Lever, Greenhouse, Workable;
  Workday after it's understood.
- Later: LLM extraction from careers pages with no known ATS (GLM, free,
  weekly, only on changed pages); enumerating Teamtailor tenants to grow the
  list without a register.

## Phase 3: news signals

- `jobsearcher/signals/news.py`: weekly Google News RSS fetch per company
  (`news_query`), stored in `news_items`, deduped by URL.
- `jobsearcher/signals/classify.py`: one LLM call per company batch of new
  items (ranking provider: GLM, free) returning per item `kind`
  (merger_acquisition | expansion | funding | leadership_change | layoffs |
  other), `relevance` 0–100 for this candidate (her M&A integration,
  operations, HR and healthcare background are in the CV context), and a
  one-line summary. Cached by item id + prompt version.
- `jobsearcher signals` CLI: fetch + classify; `--digest` prints the
  "companies to approach" list: companies ordered by their best recent
  signals, with the reason and any known contact.
- Daemon: companies/news run weekly (configurable), search + rank daily.
- After M3 is merged: a "Companies" page in the web UI; after M4: draft a
  spontaneous application for a chosen company.

## Tests (offline)

Recorded/minimal fixtures per ATS feed and for Google News RSS; detection
against small HTML fixtures; per-source expiry; Sweden filter; classification
with a fake LLM.

## Changes made while building

- **News comes from GDELT, not Google News.** Google News' robots.txt disallows
  `/rss`; GDELT is an open API built for this. It rejects quoted phrases under 4
  characters, so short or ambiguous names get a `news_query` in `companies.yaml`.
- **Company jobs are sourced per feed** (`varbi:sll`), not per company, so a
  region and its hospitals sharing one Varbi feed don't duplicate jobs.
- **Detection validates Teamtailor refs** by fetching `jobs.json`, and ignores
  shared ATS hosts (Teamtailor's `tt.`, Varbi's `feeds.`).
- **H&M** needs a manual `ats:` override: its careers site loads SmartRecruiters
  jobs with JavaScript.
