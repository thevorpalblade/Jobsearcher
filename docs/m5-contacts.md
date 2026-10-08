# M5: finding a contact person (plan, 2026-10-08)

## Why

A letter addressed to a named person, or a short note to the recruiter or the hiring
manager, gets read more often than one to "Hiring Manager". Today's coverage
(Jenny's ranked jobs, 2026-10-08):

| Jobs | Named contact person |
|---|---|
| All 893 ranked | 65% |
| Her top 30 | 23% (7) |
| Score ≥ 50 | 17% (4 of 23) |

The best matches are the ones without a name: LinkedIn ads and large employers' own
ATS feeds (Saab, Scania, Cytiva) rarely name anyone. Platsbanken ads usually do.
So M5 is about the top of the list, not the whole pool.

## Rules (from PLAN.md, unchanged)

- **Free sources only.** No paid contact databases.
- **Never invent a contact.** Every contact has a `provenance` that says where it
  was found, and the page shows it.
- **No LinkedIn scraping.** It's against their terms. LinkedIn only gets a search
  *link* for the person to open themselves.
- **Contacts are personal data about third parties.** Keep only what's on a public
  page, and drop it when the job expires (as today) or after 90 days for company
  pages.

## Sources, in the order tried

1. **What the ad already gives:** Platsbanken's `application_contacts`, emails and
   phone numbers in the text, and people the ranking model finds in the ad
   (`llm:ad_text`). Unchanged.
2. **The employer's own website** (new, the main part):
   1. **Find the site.** In order:
      - the company's `website` in the profile's `companies.yaml`;
      - Platsbanken's employer URL and JobSpy's `company_url_direct`, which we fetch
        today but don't keep (a small change to the two adapters);
      - the application email's domain, unless it's a webmail or ATS domain;
      - the apply link's host, if it isn't LinkedIn, an ATS or a job board, e.g.
        `jobs.nordicinvestin.se` gives `nordicinvestin.se`;
      - a guess from the cheap model ("the website of <company>, Sweden"), accepted
        only if the home page fetches and names the company.

      The result is stored per company and can be corrected on the job page (and is
      remembered).
   2. **Fetch a few likely pages:** the home page, then links whose text or path
      looks like contact, about, team, management, press or careers (`/kontakt`,
      `/om-oss`, `/ledning`, `/press`, `/karriar`, …). At most 6 pages per company,
      through `PoliteClient` (polite pacing, the local-address block, Chrome
      transport).
   3. **Pull people from them:**
      - schema.org `Person` / `ContactPoint` JSON-LD;
      - `mailto:` and `tel:` links;
      - the email/phone regexes;
      - one cheap model call per company over the pages' text (GLM on Z.ai,
        ≈ $0.002), asking for named people with their role, email and phone **as
        written on the page**, plus the page URL. Its answers are checked against
        the page text: a name or email that isn't literally on the page is dropped.
   4. **Cache per company domain** (`company_contacts` table, shared by every
      profile since it's public information), refreshed after 30 days.
3. **Picking the right person for a job** (one cheap model call per job, from the
   company's list): the recruiter or talent-acquisition person for that unit, or
   the likely hiring manager (head of the function the role sits in), or for a
   spontaneous application the head of that function. It returns up to 3, each with
   a one-line reason. Provenance: `company_site:<page url>`.
4. **Search links, always shown** (no network, no cost):
   - LinkedIn people search: `"<company>" recruiter OR "talent acquisition"`, and
     `"<company>" <function head title>` (e.g. "HR-chef", "Head of Operations");
   - Google: `site:linkedin.com/in "<company>" rekryterare`, and the same for the
     function head;
   - the company's own site search, when we know the site.

   They're labelled "search yourself": nothing from them is stored.

## When it runs

- **On demand:** a "Find contacts" button on a job page (and on a company page, for
  spontaneous applications). It runs in the background, like drafts, and the panel
  updates when done.
- **Automatically,** in the daily run after ranking, for each profile's jobs at or
  above `contacts.auto_min_score` in ranking.yaml (default 60), and for shortlisted
  jobs. Capped at `contacts.max_companies_per_run` (default 20), using that profile's
  model and budget.
- **Before drafting:** if a draft starts for a job with no named contact, the
  lookup runs first, so the letter can be addressed to someone.

## Web UI

- **The job page's Contacts panel,** grouped by where each contact came from:
  - From the ad.
  - From the company's website, with a link to the exact page and the model's reason.
  - Search yourself (the links).
- **Per contact:** a "use for the letter" choice, which drafting then addresses.
  Today it picks the first named person.
- **The company's website,** shown with an "edit" link to fix a wrong guess.

## Code

- `jobsearcher/contacts.py` becomes a package `jobsearcher/contacts/`, keeping
  `extract_contacts_from_text`:
  - `site.py`: find the website, fetch the pages;
  - `extract.py`: JSON-LD, links, regexes, the model pass and its literal check;
  - `pick.py`: choose people for a job;
  - `links.py`: search links.
- Store tables:
  - `company_sites (company_key, domain, source, checked_at)`;
  - `company_contacts (domain, data, fetched_at)`;
  - per-profile `contact_choices (profile, job_id, contact_key)`.
- Pipeline: a `contacts` step after `rank` in `run_pipeline`, per profile.
- Config: `contacts.auto_min_score` and `contacts.max_companies_per_run` in
  ranking.yaml.

## Phases

| # | What | Size |
|---|---|---|
| 5a | Search links on every job page, and the panel grouped by source | Small |
| 5b | Website discovery (with the adapters keeping employer URLs), page fetch, extraction, cache, "Find contacts" button | Medium |
| 5c | Picking people per job, "use for the letter", lookup before drafting | Small to medium |
| 5d | Automatic lookup for top-scoring and shortlisted jobs in the daily run | Small |

5a-5d were built on 2026-10-08 (see "As built").

## Decisions (2026-10-08)

1. **Guessed email addresses: yes, shown as a guess.** Only when the company's own
   site shows at least one real address in that pattern. The contact is labelled
   "guessed from the pattern on <page>", with provenance `pattern:company_site`.
   Drafting never sends to it: it's a suggestion for the person to check.
2. **Automatic lookups: yes,** for jobs with a final score of 60 or more and for
   shortlisted jobs (5d).
3. **Bolagsverket: try it** (5e, below).

## As built (5a-5d, 2026-10-08)

- **`jobsearcher/contacts/`:**
  - `links.py`: search links.
  - `site.py`: finding the site and the pages; candidates are tried in order, and the
    model's guess is only asked for when none of them is the company's site.
  - `extract.py`: JSON-LD, mailto links, the model pass with the literal check, and
    the address pattern.
  - `pick.py`: choosing people for a job or company.
  - `service.py`: caching, lookups, `letter_contact`, `due_for_lookup`.
- **Tables:** `company_sites` and `company_contacts` (shared), `job_contacts` (per
  profile, with the chosen contact). Jobs keep `company_url` from Platsbanken
  (`employer.url`) and JobSpy (`company_url_direct`).
- **Web:** the contacts panel (`_contacts.html`) on job and company pages. It has
  "Find contacts" (a background queue of its own), "use for the letter", setting or
  correcting the website, and the search links.
- **CLI:** `jobsearcher contacts [<job id> | --company NAME]`; with neither, it runs
  the automatic lookups. The daily run calls it after ranking, for each profile.
- **ranking.yaml:** `contacts: {auto, auto_min_score: 60, max_companies_per_run: 20,
  before_drafting: true}`.
- **Drafting:** the letter goes to the chosen contact, else a named person from the
  ad, else the first website pick. With nobody named, it looks one up first.
- **Tests stay offline:** `make_clients` refuses in tests, so tests pass fake clients.
- **First live try (4 of Jenny's top jobs, $0.003 in all):**
  - LeoVegas: its Chief HR Officer, from the executive-management page, with a
    sensible reason.
  - Tobii Dynavox, TD SYNNEX and Alstom: the site was found (guessed or from the
    apply link) and the right pages read, but large employers don't name staff there.
    The search links cover those.

## 5e: Bolagsverket (researched 2026-10-08)

- **The free API** ("värdefulla datamängder", since 2025-02-03, no agreement, no
  cost) returns name, legal form, status, address, SNI codes and **digitally filed
  annual reports**. It does **not** return officers (board, CEO, signatories). The
  fuller "API för företagsinformation" costs money, so it's out (free sources only).
- **Officers from the annual report:** the board members and the CEO sign it. The
  signatories' first name, last name and role ("Verkställande direktör",
  "Styrelseledamot") should be tagged in the iXBRL; if they aren't, they're still in
  the text, and the literal check applies. The report is up to about 18 months old,
  so the contact is shown with the report's year.
- **Use:** only for small employers (the report's average number of employees,
  `MedelantaletAnstallda`, under about 100), where the CEO is a realistic person to
  write to. Look-up by org number: Platsbanken gives `company_org_nr`; other sources
  don't, so those are matched by name, accepted only if there's exactly one hit.
- **Access:** OAuth2 client credentials, free, from a customer application at
  portal.api.bolagsverket.se (the user's step). 60 requests a minute. Keys go in
  `.env` as `BOLAGSVERKET_CLIENT_ID` / `BOLAGSVERKET_CLIENT_SECRET`.
- **Provenance:** `bolagsverket:arsredovisning:<year>`.
- **To check when building:** the exact endpoints, whether reports come as iXBRL or
  zip, and the signature element names, against the API's own documentation.

| 5e | Bolagsverket: CEO and board of small employers from their annual reports | Small to medium (after the user's application) |

