# M4: application drafting, on demand

Replaces the original "draft the top N jobs every day" design (PLAN.md §5). The user
answered the open questions on 2026-10-05; see "Decisions".

## Decisions

1. **Always English**: she doesn't speak Swedish, so there is no language option; a
   Swedish ad still gets an English letter.
2. **Auto-draft when she shortlists a job**: yes, within `drafting.max_drafts_per_day`.
3. **No in-browser editing**: she edits the downloaded Word file. The browser shows a
   **Markdown preview** (rendered letter and CV) next to the downloads.
4. **Grounding check by GLM** (the ranking model, free).

## Why on demand, not a daily batch

- **Spend where she's interested.** A batch writes letters for jobs she'd never apply
  to. Only 4 jobs score ≥75 today (8 at ≥65), and the daily top changes as the backlog
  is ranked; she knows which ones she actually wants.
- **Drafts can be steered.** "Lead with the merger integration", "mention I'm in
  Stockholm", a different reference CV as the base. A batch can't take input.
- **Subscription limits are shared** with the chat. Opus drafts for jobs nobody opens
  burn the allowance for nothing.
- **Fresher and fit for spontaneous applications**: the same flow drafts an unsolicited
  application to a company from its news signal, which has no ad to batch over.
- Cost of on-demand: she waits 1–3 minutes after clicking. Mitigation below: drafting
  starts by itself when she **shortlists** a job (a clear sign of interest), so it's
  often ready when she opens it.

No daily batch. `drafting.max_drafts_per_day` stays only as a safety cap on automatic
(shortlist-triggered) drafts.

## What a draft contains

One generation call returns structured JSON (`claude -p --json-schema`):

- `cover_letter` (Markdown), in English, addressed to the named contact when
  the job has one with a person's name (never a generic mailbox, never invented),
  otherwise a neutral salutation.
- `cv` (Markdown): the master CV (or the chosen reference CV) re-ordered and trimmed for
  this role: summary rewritten, bullets selected and reworded from facts already in the
  CVs, keywords from the ad used where they're true.
- `notes` for her: what was emphasised, which requirements aren't covered by her CV, and
  questions only she can answer (e.g. "the ad asks for X; do you have it?").

Inputs: master CV and the other CVs in `cvs/` as extra facts, the ad (title, company,
description, deadline, contacts), the ranking (matched/missing requirements, language,
Swedish requirement), her standing instructions (`draft_instructions` in profile.yaml,
for every draft, e.g. which phone number to use where) and her optional instructions
for this draft. Chat context isn't used.

The letter's look is profile.yaml's `letter:` section: `font`, `greeting` ("To" gives
"To the Hiring Manager,") and `header` (a letterhead: name, email | phone, "Re: <role>",
today's date, which is passed to the model and counted as a known figure). A signature
image uploaded on Settings (`signature.png|jpg` next to the profile's `cvs/`) goes above
the letter's last line, the signed name, in the Word and PDF files.

## The hard rule: never invent experience

1. The generation prompt forbids new employers, titles, dates, numbers, skills,
   certifications and contact details; it may rephrase and select, not add.
2. **Grounding check** (second call, a different model so it isn't marking its own
   homework: the ranking model, GLM, which is free): lists every factual claim in the CV
   and letter and, for each, a supporting quote from the CVs or `unsupported`.
3. **Repair loop:** if anything is unsupported, one more generation with the flagged claims
   fed back ("remove or fix these"), then re-check.
4. Still flagged after that: the draft is saved but marked **needs review**, the flagged
   claims are shown in red on the draft page, and the PDF/Word files carry no hidden
   changes. Nothing is ever silently shipped.

## Data model and caching

- Table `drafts` already exists: `(job_id, input_hash, data, created_at)`. `data` is the
  JSON above plus `model`, `prompt_version`, `instructions`, `grounding`
  (claims, flagged, repaired) and file names. Add `drafts.kind` ("job" | "company") and a
  nullable `company` for spontaneous drafts (new columns, migrated).
- `input_hash` = hash(ad content, all CV texts, the ranking's requirement lists,
  standing and per-draft instructions, model, prompt version): identical requests are free; changed
  instructions or CVs make a new version, and older versions stay listed.
- Job status in the UI: none / queued / running / ready / needs review / failed.

## Rendering

- **Word:** python-docx (already a dependency) from the Markdown, with one clean template
  for the CV and one for the letter (headings, bullets, bold, sensible margins, a font
  that has åäö).
- **PDF:** the same .docx converted with headless LibreOffice (`soffice --headless
  --convert-to pdf`), so Word and PDF always match. If `soffice` is missing, only the
  Word and Markdown files are produced and the page says so.
- Files go to `data/drafts/<job_id>/<version>/` (`cv.md|docx|pdf`, `letter.md|docx|pdf`).
  Names for download: `<Name>-CV-<Company>.pdf`.

### Check reliability (found in the first live run)

The GLM check on NVIDIA's free tier answered with 504s for ~30 minutes while the daemon
was ranking (every call waits in the same queue). So the check has a 150 s time limit and
no SDK retries, and an optional `llm.grounding_fallback` (Claude Haiku on the subscription)
answers instead when GLM is slow or down; the re-check after a repair starts with whoever
answered. If every checker fails, the draft is saved as "needs review" (check couldn't run).

## Where it's used

- **Job page** (`{% block drafts %}`): a "Draft application" panel: optional
  instructions, base CV (master or a reference CV), a button. While running it polls
  (HTMX); when ready it shows the letter and CV rendered, the notes, any red flagged
  claims, and download buttons. "Regenerate with instructions" makes a new version.
- **Dashboard top five:** a "Draft" button on each card.
- **Shortlisting** a job queues a draft automatically (`drafting.auto_on_shortlist`,
  default on, within `max_drafts_per_day`).
- **Chat:** the chat's Claude is told to use `jobsearcher draft` for application
  documents instead of free-handing them, so chat drafts get the same grounding check.
- **CLI:** `jobsearcher draft <job_id> [--instructions "..."]
  [--cv NAME]`, `jobsearcher draft --company "Name"` (spontaneous), `jobsearcher drafts`
  (list). The web button runs the same code path in a background thread of the web
  process, using the drafting model (Claude Code on the subscription, no tools).
- **Spontaneous applications:** from a company's signal on the dashboard
  ("companies worth a spontaneous application") a "Draft" button writes an unsolicited
  letter that cites the news item (and only that) as the reason for writing.

## Limits and cost

One draft runs at a time (own queue, separate from the chat's single run). A generation
is one Opus call (a few thousand output tokens) plus a free GLM check, plus at most one
repair round. Failures (usage limit reached, timeouts) show on the draft with a retry;
nothing is retried automatically against the subscription.

## Tests (offline)

Fake LLMs for generation and the check: structured output parsing, addressee logic
(named person vs generic mailbox vs none), repair loop (fixed on second pass, still
flagged, never flagged), cache hits and new versions, docx round-trip (python-docx
reads back the text), PDF conversion skipped cleanly without `soffice`, the job-page
panel and its polling states, auto-draft on shortlist and its daily cap, spontaneous
drafts, the chat prompt mentioning `jobsearcher draft`.

## Build order

1. `drafting/` module: prompt, schema, generate, cache, `jobsearcher draft` CLI (no UI).
2. Grounding check and repair loop.
3. Rendering (docx, then PDF via soffice).
4. Background runner + job-page panel with polling and downloads.
5. Dashboard button, shortlist trigger, daily cap.
6. Spontaneous drafts from signals.
7. Chat integration (system prompt) and docs.
