# M8: more job sources

Approved by the user on 2026-10-05: build items 1–4 below, plan first.

Context: the user runs Jobsearcher on one computer for one person, at low volume,
and has decided that crawling may use a Chrome user agent and ignore robots.txt.
Per-host pacing stays: it keeps the home IP from being blocked.

## Findings that shape the plan (live checks, 2026-10-05)

- **JobSpy** (`python-jobspy` 1.2.0, released 2026-10-02) scraped LinkedIn and
  Indeed Sweden without a login: 8 + 8 "HR Business Partner" jobs in Stockholm with
  full descriptions in 7 s. It impersonates a browser (curl_cffi). Depends on
  pandas, so it's an optional extra.
- **Workday** answers its career sites' job API (`POST
  https://<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs`) once the
  request has a browser user agent plus `Origin`/`Referer` headers (earlier 422s
  lacked them). Essity, Sandvik, Husqvarna and Saab all returned jobs; Sandvik's
  first Stockholm hit was "Senior Integration Manager, M&A".
- **Robots.txt** blocked Google News RSS and MFN, and ~9 company sites refused the
  honest user agent (Volvo Cars, Ericsson, PostNord, AstraZeneca, Hexagon, Epiroc,
  Getinge, Kriminalvården, Göteborgs stad).

## 1. Crawl settings: user agent and robots.txt

- `config.yaml` gets a `crawl` section:
  ```yaml
  crawl:
    user_agent: chrome          # chrome | honest | <any literal UA string>
    respect_robots: false       # true: skip URLs robots.txt disallows
  ```
  Code defaults stay conservative (`honest`, `true`); the user's config sets
  `chrome` / `false`. `PoliteClient` and the source HTTP clients take the UA from
  here; `PoliteClient` only reads robots.txt when `respect_robots` is true. Per-host
  pacing is unchanged.
- **News:** `companies.news_source: auto | google_news | gdelt`. `auto` uses Google
  News RSS (much better Swedish coverage, no 6 s limit) when robots.txt isn't
  respected, else GDELT. Google News items: title, link, published, source name.
- Re-detect companies whose last detection failed (403/401/no ATS) on the next
  run, so the Chrome UA gets a chance: `jobsearcher companies --detect --failed`.
- Small fix bundled here: CLI summary lines use line-buffered stdout, so the
  daemon's log shows them as they happen.
- CLAUDE.md's "don't work around robots.txt" rule becomes "follow the `crawl`
  settings".

## 2. Recruitment agencies as target companies

Add to `companies.example.yaml` (and the user's `companies.yaml`), tag
`recruiter`, source `curated:recruiters`: Wise Professionals (HR specialists),
Perido (HR consultancy, seen in ranked ads), Academic Work, Poolia, PageGroup /
Michael Page, Hays, Mercuri Urval, Randstad, Adecco, Experis/ManpowerGroup,
Bravura, Novare, Clockwork. No code: detection finds their ATS where supported.
Their consulting roles are scored down by the LLM via the existing "short
consulting assignments" dislike.

## 3. LinkedIn and Indeed via JobSpy

- Optional extra `jobspy = ["python-jobspy>=1.2"]`; the Docker image installs it.
- `config.yaml`:
  ```yaml
  sources:
    jobspy:
      sites: [linkedin, indeed]   # empty = off; also glassdoor, google
      location: Sweden            # the location filter then keeps your cities
      hours_old: 168              # only ads posted in the last week, each run
      results_per_search: 25
      max_age_days: 30            # expire ads this old (see below)
      pause_s: 5                  # between searches, against rate limiting
  ```
- `jobsearcher/sources/jobspy_source.py`: one `SourceAdapter` per site
  (`name = "linkedin"` / `"indeed"`), `search(keyword)` → `scrape_jobs(...)` with
  descriptions on (`linkedin_fetch_description`, markdown). Mapping: id, title,
  company, location → **city only** (LinkedIn says "Stockholm, Stockholm County,
  Sweden") so dedupe merges with Platsbanken/company feeds; region; remote; full
  description; job URL; direct apply URL; date posted; contacts from the text with
  provenance `linkedin:ad_text` / `indeed:ad_text`.
- **Expiry:** these sources only return recent ads (`hours_old`), so "not seen
  for 3 days" doesn't mean "filled". Jobs found only on JobSpy sources are expired
  by age (`max_age_days` after posting) instead; `expire_jobs` skips them.
- **Blocking:** a 429 or block fails that site for the run (logged); its jobs are
  kept. Keywords run sequentially with `pause_s` between them.
- Not installed → the source is skipped with a warning, not a crash.

## 4. Workday, and a generic job-posting reader

- **Workday adapter** (`sources/ats/workday.py`, type `workday`): detection already
  records refs like `essity.wd3.myworkdayjobs.com/en-US/Job_opportunities`; parse
  tenant, `wdN` and site. List with the CXS `POST .../jobs` (paged, `limit` 20,
  `searchText` empty), keep Swedish locations (`locationsText`, falls back to the
  detail page's country), fetch details (`GET .../wday/cxs/<tenant>/<site><externalPath>`)
  only for wanted jobs (the `Wanted` hook, as SmartRecruiters does). Browser UA +
  `Origin`/`Referer` headers.
- **Generic JSON-LD reader** (type `jsonld`): many careers pages embed schema.org
  `JobPosting` JSON-LD on each job page. Given a careers page, collect same-site
  links that look like job ads, fetch each (capped, e.g. 60 per company), and parse
  `JobPosting` blocks (title, description, `jobLocation`, `datePosted`,
  `validThrough`, `hiringOrganization`). Detection falls back to it when a
  careers page (or one job link from it) has JobPosting JSON-LD.
- **Headless browser (optional extra `[browser]`, Playwright/Chromium)** for
  careers pages that only render with JavaScript (H&M-style): used by detection
  and the JSON-LD reader when the static page has no links/ATS and Playwright is
  installed. Skipped cleanly when not installed. Not in the Docker image by
  default (≈300 MB); a build arg turns it on.
- Out of scope here (noted in REMAINING_WORK): ReachMee, Jobylon, SuccessFactors
  adapters, unless JSON-LD covers them.

## Tests (offline)

Recorded/synthetic fixtures for: crawl settings (UA header, robots on/off),
Google News RSS parsing, JobSpy mapping (a stub `scrape_jobs` returning a
DataFrame-like table; skipped if pandas isn't installed), age-based expiry,
Workday list/detail mapping and Sweden filter, JSON-LD extraction from HTML
fixtures, detection choosing workday/jsonld.

## Rollout

Build in a worktree (the daemon runs from the main checkout), merge to `main`,
install `.[jobspy]` in the main venv, update the user's `config.yaml`
(`crawl`, `sources.jobspy`) and `companies.yaml` (recruiters), restart the
daemon, re-detect failed companies, and report what each new source added.

## Changes made while building

- **A Chrome User-Agent header wasn't enough**: bot protection fingerprints the
  TLS handshake. With `crawl.user_agent: chrome`, `PoliteClient` sends requests
  through curl_cffi impersonating Chrome (installed with the `jobspy` extra); then
  Volvo Cars, Ericsson, PostNord, Poolia and AstraZeneca all answered.
- **Recruiters**: none use a supported ATS. Randstad's ads carry JobPosting
  JSON-LD (detected as `jsonld`); the others' don't in their server HTML.
- **Workday** facets nest on some sites (Saab), and some sites have no country
  filter (Apotek Hjärtat); both are handled. Postings listed in several places
  are fetched first (capped) to learn their city.
- **Headless browser: deferred** (see REMAINING_WORK §6).
